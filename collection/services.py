"""The ONLY code allowed to mutate Holding.quantity.

Every change happens in one transaction together with its QuantityChange audit
row, so the ledger and its history can never diverge — the failure class that
repeatedly corrupted the legacy JSON ledger.

Holdings are always resolved by (owner, card) so the multi-user future the
schema provides for cannot cross-mutate another owner's rows.
"""

import uuid
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from catalog.models import Printing

from .models import Holding, QuantityChange, SellPackage, SellPackageItem


class QuantityError(ValueError):
    pass


def _owner(user):
    return user if getattr(user, "pk", None) else None


def _to_decimal(value):
    """Coerce a form value to a usable Decimal, or raise QuantityError.
    Django would raise ValidationError deep in save(); catch it at the door."""
    if value in (None, ""):
        return None
    if isinstance(value, Decimal):
        candidate = value
    else:
        try:
            candidate = Decimal(str(value).strip().replace(",", "."))
        except InvalidOperation:
            raise QuantityError(f"“{value}” is not a valid price.") from None
    if not candidate.is_finite() or candidate < 0:
        raise QuantityError(f"“{value}” is not a valid price.")
    return candidate.quantize(Decimal("0.01"))


def _locked_holding(card: Printing, user) -> Holding | None:
    return (
        Holding.objects.select_for_update()
        .filter(card=card, owner=_owner(user))
        .first()
    )


def set_quantity(
    card: Printing,
    new_quantity: int,
    *,
    user,
    reason: str = QuantityChange.Reason.MANUAL_EDIT,
    note: str = "",
    sell_package=None,
    batch_id=None,
) -> tuple[Holding, QuantityChange | None]:
    """Set the owned quantity of one printing, audited. Returns (holding, change);
    change is None when the value did not actually change."""
    if new_quantity < 0:
        raise QuantityError("Quantity cannot be negative.")
    if new_quantity > 999:
        raise QuantityError("Quantity out of range (max 999).")

    with transaction.atomic():
        holding = _locked_holding(card, user)
        if holding is None:
            holding = Holding.objects.create(card=card, owner=_owner(user), quantity=0)
        return _apply_quantity(
            holding, new_quantity, user=user, reason=reason, note=note,
            sell_package=sell_package, batch_id=batch_id,
        )


def _apply_quantity(holding, new_quantity, *, user, reason, note="", sell_package=None,
                    batch_id=None):
    """Write one quantity change: holding + audit row, same transaction."""
    old = holding.quantity
    if old == new_quantity:
        return holding, None

    holding.quantity = new_quantity
    holding.save(update_fields=["quantity", "updated_at"])

    change = QuantityChange.objects.create(
        holding=holding,
        card_id=holding.card_id,
        old_quantity=old,
        new_quantity=new_quantity,
        delta=new_quantity - old,
        reason=reason,
        note=note,
        sell_package=sell_package,
        user=_owner(user),
        batch_id=batch_id,
    )
    return holding, change


def adjust_quantity(card: Printing, delta: int, *, user, reason=QuantityChange.Reason.MANUAL_EDIT,
                    note: str = "") -> tuple[Holding, QuantityChange | None]:
    """Stepper +/- helper: clamps at 0 instead of erroring below zero."""
    with transaction.atomic():
        holding = _locked_holding(card, user)
        current = holding.quantity if holding else 0
        return set_quantity(card, max(0, current + delta), user=user, reason=reason, note=note)


def reserved_quantities(card_ids, *, user) -> dict[int, int]:
    """Copies committed to keep packages, per card.

    This replaces Holding.keeper_quantity: reserving a card is now just adding
    it to a package with kind=KEEP, so there is one mechanism instead of two.
    Cancelled keep packages release their reservation.
    """
    rows = (
        SellPackageItem.objects.filter(
            card_id__in=list(card_ids),
            package__kind=SellPackage.Kind.KEEP,
            package__owner=_owner(user),
        )
        .exclude(package__status=SellPackage.Status.CANCELLED)
        .values_list("card_id")
        .annotate(total=Sum("quantity"))
    )
    return {card_id: total for card_id, total in rows}


def reserved_quantity(card: Printing, *, user) -> int:
    return reserved_quantities([card.pk], user=user).get(card.pk, 0)


def preview_bulk(cards, op: str, value: int, *, user) -> dict:
    """Impact summary for the confirm dialog — computed exactly the way apply
    computes it, so the numbers shown are the numbers changed."""
    cards = list(cards)
    targets = _bulk_targets(cards, op, value, user=user)
    changing = [(card, current, new) for card, current, new in targets if current != new]
    return {
        "total": len(cards),
        "changing": changing,
        "unchanged": len(cards) - len(changing),
    }


def _bulk_targets(cards, op: str, value: int, *, user) -> list:
    """(card, current_qty, new_qty) for every card in the scope, scoped to the
    acting user's own holdings."""
    if op not in ("set", "inc", "zero"):
        raise QuantityError(f"Unknown bulk operation: {op}")
    if op == "set" and not 0 <= value <= 999:
        raise QuantityError("Quantity out of range (0–999).")
    holdings = {
        h.card_id: h.quantity
        for h in Holding.objects.filter(card__in=cards, owner=_owner(user))
    }
    targets = []
    for card in cards:
        current = holdings.get(card.pk, 0)
        if op == "set":
            new = value
        elif op == "inc":
            new = min(999, current + 1)
        else:  # zero
            new = 0
        targets.append((card, current, new))
    return targets


def apply_bulk(cards, op: str, value: int, *, user, note: str = ""):
    """Apply one bulk operation atomically. Returns (batch_id, changed_count).
    Every change carries the same batch_id so the whole action undoes in one
    click; unchanged cards get no audit noise."""
    batch_id = uuid.uuid4()
    changed = 0
    with transaction.atomic():
        for card, current, new in _bulk_targets(list(cards), op, value, user=user):
            if current == new:
                continue
            _, change = set_quantity(
                card, new, user=user, reason=QuantityChange.Reason.BULK_EDIT,
                note=note, batch_id=batch_id,
            )
            if change is not None:
                changed += 1
    return batch_id, changed


def undo_batch(batch_id, *, user) -> tuple[int, int]:
    """Revert every change in a bulk batch (inverse writes, nothing deleted).
    Changes superseded by a later edit are skipped, not overwritten.
    Returns (reverted, skipped)."""
    undo_batch_id = uuid.uuid4()
    reverted = skipped = 0
    with transaction.atomic():
        # Materialize first: the loop writes new QuantityChange rows, and a lazy
        # queryset could otherwise pick up this undo's own rows.
        changes = list(
            QuantityChange.objects.filter(batch_id=batch_id).select_related("holding")
        )
        for change in changes:
            holding = Holding.objects.select_for_update().get(pk=change.holding_id)
            if holding.quantity != change.new_quantity:
                skipped += 1
                continue
            _apply_quantity(
                holding, change.old_quantity, user=user,
                reason=QuantityChange.Reason.UNDO,
                note=f"undo of bulk batch {batch_id}",
                batch_id=undo_batch_id,
                )
            reverted += 1
    return reverted, skipped


def finalize_sale(package, *, user, sold_price=None, buyer_name: str = ""):
    """Mark a package sold: validates every item against sellable quantity
    (owned minus anything reserved in a keep package), then decrements holdings
    through the audited write path — the replacement for 'delete cards from the
    JSON after a sale'."""
    with transaction.atomic():
        package = SellPackage.objects.select_for_update().get(pk=package.pk)
        if package.kind == SellPackage.Kind.KEEP:
            raise QuantityError("A keep package is not for sale.")
        if package.status == SellPackage.Status.SOLD:
            raise QuantityError("Package is already sold.")
        items = list(package.items.select_related("card"))
        if not items:
            raise QuantityError("Package has no items.")

        problems = []
        # Scoped to the seller: Holding is unique on (owner, card), so an
        # unscoped lookup would validate and decrement an arbitrary owner's row.
        holdings = {
            h.card_id: h
            for h in Holding.objects.select_for_update().filter(
                owner=_owner(user), card_id__in=[i.card_id for i in items]
            )
        }
        reserved = reserved_quantities([i.card_id for i in items], user=user)
        for item in items:
            holding = holdings.get(item.card_id)
            owned = holding.quantity if holding else 0
            sellable = max(0, owned - reserved.get(item.card_id, 0))
            if item.quantity > sellable:
                problems.append(
                    f"{item.card.display_name}: selling {item.quantity}, "
                    f"but only {sellable} sellable (owned minus reserved)"
                )
        if problems:
            raise QuantityError("Cannot finalize sale — " + "; ".join(problems))

        batch_id = uuid.uuid4()
        for item in items:
            holding = holdings[item.card_id]
            _apply_quantity(
                holding, holding.quantity - item.quantity, user=user,
                reason=QuantityChange.Reason.SALE,
                note=f"sold in package: {package.name}",
                sell_package=package, batch_id=batch_id,
            )
        package.status = SellPackage.Status.SOLD
        package.sold_price = _to_decimal(sold_price)
        package.buyer_name = buyer_name or package.buyer_name
        package.sold_at = timezone.now()
        package.save(update_fields=["status", "sold_price", "buyer_name", "sold_at", "updated_at"])
    return package


def undo_change(change: QuantityChange, *, user) -> tuple[Holding, QuantityChange | None]:
    """Revert one audit entry by writing an inverse change (never by deleting).

    Guarded against stale undo: if the holding's quantity has moved on since
    this change was written (another edit, another tab), refuse instead of
    silently overwriting the newer value.
    """
    with transaction.atomic():
        holding = (
            Holding.objects.select_for_update().get(pk=change.holding_id)
        )
        if holding.quantity != change.new_quantity:
            raise QuantityError(
                f"Quantity is now {holding.quantity}, not {change.new_quantity} — "
                "this change was already superseded. Set the value directly instead."
            )
        return _apply_quantity(
            holding,
            change.old_quantity,
            user=user,
            reason=QuantityChange.Reason.UNDO,
            note=f"undo of change #{change.pk}",
        )


# -- rule-based package building ---------------------------------------------

PRICE_BASES = {
    "trend": "current_price_trend",
    "from": "current_price_from",
    "30d": "current_price_30d",
}


def package_candidates(
    package, *, user, basis: str = "trend", min_price=None, max_price=None,
    expansion_slug: str = "", archetype: str = "", keep_each: int = 0,
) -> list[dict]:
    """Cards a rule would add to a package, with the quantity of each.

    The arithmetic that matters: a copy is only offered if it is genuinely
    spare — owned, minus anything reserved in a keep package, minus what this
    package already holds, minus however many of each you want to keep back.
    That is what makes "sell my duplicates" (keep_each=1) safe to run.
    """
    field = PRICE_BASES.get(basis, PRICE_BASES["trend"])
    holdings = (
        Holding.objects.filter(
            owner=_owner(user), quantity__gt=0, card__expansion__game=package.game
        )
        .select_related("card", "card__expansion", "card__piece")
    )
    if expansion_slug:
        holdings = holdings.filter(card__expansion__slug=expansion_slug)
    if archetype:
        holdings = holdings.filter(card__piece__archetype=archetype)
    if min_price is not None:
        holdings = holdings.filter(**{f"card__{field}__gte": min_price})
    if max_price is not None:
        holdings = holdings.filter(**{f"card__{field}__lte": max_price})
    if min_price is not None or max_price is not None:
        # A card with no price at all can't satisfy a price rule.
        holdings = holdings.exclude(**{f"card__{field}__isnull": True})

    holdings = list(holdings.order_by("card__expansion__slug", "card__slug"))
    card_ids = [h.card_id for h in holdings]
    reserved = reserved_quantities(card_ids, user=user)
    already = dict(
        package.items.filter(card_id__in=card_ids).values_list("card_id", "quantity")
    )

    candidates = []
    for holding in holdings:
        spare = (
            holding.quantity
            - reserved.get(holding.card_id, 0)
            - already.get(holding.card_id, 0)
            - max(0, keep_each)
        )
        if spare <= 0:
            continue
        price = getattr(holding.card, field)
        candidates.append({
            "card": holding.card,
            "quantity": spare,
            "unit_price": price,
            "line_total": (price or Decimal("0")) * spare,
            "owned": holding.quantity,
        })
    return candidates


def preview_package_rule(package, *, user, **rule) -> dict:
    """Impact summary, computed exactly the way apply computes it."""
    candidates = package_candidates(package, user=user, **rule)
    return {
        "candidates": candidates,
        "cards": len(candidates),
        "copies": sum(c["quantity"] for c in candidates),
        "total": sum(c["line_total"] for c in candidates),
        "sample": candidates[:12],
        "more": max(0, len(candidates) - 12),
    }


def apply_package_rule(package, *, user, **rule) -> tuple[int, int]:
    """Add every matching card. Returns (cards added, copies added).

    Prices freeze at the moment of adding, exactly as a manual add does — a
    rule is a faster way to pick cards, not a different kind of listing.
    """
    candidates = package_candidates(package, user=user, **rule)
    if not candidates:
        raise QuantityError("Nothing matches that rule.")

    copies = 0
    with transaction.atomic():
        # Re-read the status under lock: the caller may be holding an object
        # loaded before the package was sold, and a stale in-memory copy must
        # not be able to slip items into a frozen package.
        locked = SellPackage.objects.select_for_update().get(pk=package.pk)
        if locked.status == SellPackage.Status.SOLD:
            raise QuantityError("This package is sold — its contents are frozen.")
        existing = {
            item.card_id: item
            for item in package.items.select_for_update().filter(
                card_id__in=[c["card"].pk for c in candidates]
            )
        }
        to_create, to_update = [], []
        for candidate in candidates:
            card, quantity = candidate["card"], candidate["quantity"]
            copies += quantity
            item = existing.get(card.pk)
            if item is None:
                to_create.append(SellPackageItem(
                    package=package, card=card, quantity=quantity,
                    # Keep-package items carry no price: they are not for sale.
                    unit_price_at_listing=(
                        None if package.kind == SellPackage.Kind.KEEP
                        else candidate["unit_price"]
                    ),
                ))
            else:
                # Topping up keeps the original listing price, as manual adds do.
                item.quantity += quantity
                to_update.append(item)
        SellPackageItem.objects.bulk_create(to_create)
        if to_update:
            SellPackageItem.objects.bulk_update(to_update, ["quantity"])
    return len(candidates), copies
