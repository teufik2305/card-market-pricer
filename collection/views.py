from django.db.models import (
    Case,
    Count,
    F,
    IntegerField,
    Q,
    Sum,
    When,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from catalog.models import Expansion, Game, Printing
from catalog.normalize import normalize_name

from . import services
from .models import Holding, QuantityChange, SellPackage, SellPackageItem
from .services import QuantityError

QUICK_ADD_LIMIT = 25


def _annotated(card_pk) -> Printing:
    return (
        Printing.objects.annotate(
            qty=Sum("holdings__quantity")
        ).get(pk=card_pk)
    )


def _qty_response(request, card, change, error: str = ""):
    """The canonical qty-cell partial plus an OOB toast (with undo) on change.
    Sends HX-Trigger: qty-changed so totals fragments can refresh themselves."""
    response = render(request, "collection/partials/qty_cell.html", {
        "printing": _annotated(card.pk),
        "change": change,
        "error": error,
    })
    if change is not None:
        response["HX-Trigger"] = "qty-changed"
    return response


@require_POST
def update_quantity(request, card_id):
    card = get_object_or_404(Printing, pk=card_id)
    op = request.POST.get("op", "set")
    try:
        if op == "inc":
            _, change = services.adjust_quantity(card, +1, user=request.user)
        elif op == "dec":
            _, change = services.adjust_quantity(card, -1, user=request.user)
        else:
            value = int(request.POST.get("value", "0") or 0)
            _, change = services.set_quantity(card, value, user=request.user)
    except (QuantityError, ValueError) as exc:
        return _qty_response(request, card, None, error=str(exc))
    return _qty_response(request, card, change)


@require_POST
def undo_change(request, pk):
    change = get_object_or_404(QuantityChange, pk=pk)
    try:
        _, new_change = services.undo_change(change, user=request.user)
    except QuantityError as exc:
        return _qty_response(request, change.card, None, error=str(exc))
    return _qty_response(request, change.card, new_change)


# -- bulk actions -------------------------------------------------------------

def _bulk_scope(request):
    """Resolve the bulk target set: exactly the rows currently visible on the
    expansion page (same filters), so the confirm dialog cannot lie."""
    from catalog.views import expansion_card_queryset

    game = get_object_or_404(Game, code=request.POST.get("game", ""))
    expansion = get_object_or_404(
        Expansion, game=game, slug=request.POST.get("expansion", "")
    )
    q = request.POST.get("q", "").strip()
    owned_only = request.POST.get("owned") == "1"
    cards = list(expansion_card_queryset(expansion, q, owned_only))
    return game, expansion, cards, q, owned_only


def _bulk_value(request) -> int:
    """An empty field is an error, never a silent 0 — 'set all to <blank>'
    must not mean 'zero everything'."""
    raw = request.POST.get("value", "").strip()
    if raw == "":
        raise QuantityError("Enter a quantity first.")
    return int(raw)


@require_POST
def bulk_preview(request):
    game, expansion, cards, q, owned_only = _bulk_scope(request)
    op = request.POST.get("op", "set")
    try:
        value = _bulk_value(request)
        preview = services.preview_bulk(cards, op, value, user=request.user)
    except (QuantityError, ValueError) as exc:
        return render(request, "collection/partials/bulk_error.html", {"error": str(exc)})
    return render(request, "collection/partials/bulk_confirm.html", {
        "game": game, "expansion": expansion, "op": op, "value": value,
        "q": q, "owned_only": owned_only, "preview": preview,
        "sample": preview["changing"][:12],
        "more": max(0, len(preview["changing"]) - 12),
    })


@require_POST
def bulk_apply(request):
    from catalog.views import expansion_panel_context

    game, expansion, cards, q, owned_only = _bulk_scope(request)
    op = request.POST.get("op", "set")
    try:
        value = _bulk_value(request)
        batch_id, changed = services.apply_bulk(
            cards, op, value, user=request.user,
            note=f"bulk {op} on {expansion.slug}",
        )
    except (QuantityError, ValueError) as exc:
        return render(request, "collection/partials/bulk_error.html", {"error": str(exc)})

    context = expansion_panel_context(request, game, expansion, q, owned_only)
    context.update({"batch_id": batch_id, "batch_changed": changed})
    response = render(request, "catalog/partials/card_panel.html", context)
    response["HX-Trigger"] = "qty-changed"
    return response


@require_POST
def bulk_undo(request, batch_id):
    from catalog.views import expansion_panel_context

    game, expansion, _cards, q, owned_only = _bulk_scope(request)
    reverted, skipped = services.undo_batch(batch_id, user=request.user)
    context = expansion_panel_context(request, game, expansion, q, owned_only)
    context.update({"undo_reverted": reverted, "undo_skipped": skipped})
    response = render(request, "catalog/partials/card_panel.html", context)
    response["HX-Trigger"] = "qty-changed"
    return response


# -- quick add ----------------------------------------------------------------

def quick_add(request):
    """Global search modal: find any printing across games, set its quantity
    inline. For loose cards, without hunting for the right set first."""
    q = request.GET.get("q", "").strip()
    results = []
    if len(q) >= 3:
        results = list(
            Printing.objects.filter(name_normalized__contains=normalize_name(q))
            .select_related("expansion", "expansion__game")
            .annotate(qty=Sum("holdings__quantity"))
            .order_by(F("qty").desc(nulls_last=True), "-current_price_trend", "slug")[
                :QUICK_ADD_LIMIT
            ]
        )
    return render(request, "collection/partials/quick_add.html", {
        "q": q, "results": results, "too_short": 0 < len(q) < 3,
    })


# -- sell packages ------------------------------------------------------------

STATUS_RANK = Case(
    When(status=SellPackage.Status.DRAFT, then=0),
    When(status=SellPackage.Status.LISTED, then=1),
    When(status=SellPackage.Status.SOLD, then=2),
    default=3,  # cancelled last
    output_field=IntegerField(),
)


def package_list(request, game):
    game = get_object_or_404(Game, code=game)
    packages = (
        SellPackage.objects.filter(game=game)
        .annotate(item_count=Count("items"), copy_count=Sum("items__quantity"))
        .alias(rank=STATUS_RANK)
        # Keep packages first: they are the standing reservation, not a transaction.
        .order_by("-kind", "rank", "-created_at")
    )
    return render(request, "collection/package_list.html", {
        "game": game, "packages": packages,
    })


@require_POST
def package_create(request, game):
    game = get_object_or_404(Game, code=game)
    name = request.POST.get("name", "").strip() or "Untitled package"
    kind = request.POST.get("kind", SellPackage.Kind.SELL)
    if kind not in SellPackage.Kind.values:
        kind = SellPackage.Kind.SELL
    package = SellPackage.objects.create(
        name=name, game=game, kind=kind,
        owner=request.user if request.user.pk else None,
    )
    return redirect("package-detail", game=game.code, pk=package.pk)


def _get_package(game, pk) -> SellPackage:
    """Packages belong to one game — never resolve one from another game's URL."""
    return get_object_or_404(SellPackage, pk=pk, game=game)


def _package_context(game, package, user=None) -> dict:
    items = list(
        package.items.select_related("card", "card__expansion").order_by("card__slug")
    )
    owner = user if getattr(user, "pk", None) else package.owner
    holdings = {
        h.card_id: h
        for h in Holding.objects.filter(
            card_id__in=[i.card_id for i in items], owner=owner
        )
    }
    # Copies reserved elsewhere reduce what this package may commit — but a keep
    # package must not be measured against itself.
    reserved = services.reserved_quantities([i.card_id for i in items], user=owner)
    if package.kind == SellPackage.Kind.KEEP:
        for item in items:
            reserved[item.card_id] = reserved.get(item.card_id, 0) - item.quantity

    rows = []
    total_listed = total_current = 0
    for item in items:
        holding = holdings.get(item.card_id)
        owned = holding.quantity if holding else 0
        sellable = max(0, owned - max(0, reserved.get(item.card_id, 0)))
        listed = (item.unit_price_at_listing or 0) * item.quantity
        current = (item.card.current_price_trend or 0) * item.quantity
        total_listed += listed
        total_current += current
        rows.append({
            "item": item, "sellable": sellable,
            "over_committed": item.quantity > sellable,
            "line_listed": listed, "line_current": current,
        })
    return {
        "game": game, "package": package, "rows": rows,
        "total_listed": total_listed, "total_current": total_current,
        "blocked": any(r["over_committed"] for r in rows) or not rows,
    }


def package_detail(request, game, pk):
    game = get_object_or_404(Game, code=game)
    package = _get_package(game, pk)
    return render(request, "collection/package_detail.html",
                  _package_context(game, package, request.user))


def _package_panel(request, game, package, error: str = "", status: int = 200):
    context = _package_context(game, package, request.user)
    context["error"] = error
    return render(request, "collection/partials/package_panel.html", context, status=status)


@require_POST
def package_add_item(request, game, pk):
    game = get_object_or_404(Game, code=game)
    package = _get_package(game, pk)
    if package.status == SellPackage.Status.SOLD:
        return _package_panel(request, game, package,
                              error="This package is sold — its contents are frozen.")
    # Cards must belong to this package's game.
    card = get_object_or_404(Printing, pk=request.POST.get("card_id"), expansion__game=game)
    try:
        quantity = int(request.POST.get("quantity", "1") or 1)
    except ValueError:
        quantity = 1
    if not 1 <= quantity <= 999:
        return _package_panel(request, game, package,
                              error="Quantity must be between 1 and 999.")

    item, created = SellPackageItem.objects.get_or_create(
        package=package, card=card,
        defaults={
            "quantity": quantity,
            # Freeze the price at listing time — the market moves, the deal shouldn't.
            "unit_price_at_listing": card.current_price_trend,
        },
    )
    if not created:
        # Topping up an existing line keeps the original listing price.
        item.quantity = min(999, item.quantity + quantity)
        item.save(update_fields=["quantity"])
    return _package_panel(request, game, package)


@require_POST
def package_remove_item(request, game, pk, item_id):
    game = get_object_or_404(Game, code=game)
    package = _get_package(game, pk)
    if package.status == SellPackage.Status.SOLD:
        return _package_panel(request, game, package,
                              error="This package is sold — its contents are frozen.")
    package.items.filter(pk=item_id).delete()
    return _package_panel(request, game, package)


@require_POST
def package_finalize(request, game, pk):
    game = get_object_or_404(Game, code=game)
    package = _get_package(game, pk)
    error = ""
    try:
        services.finalize_sale(
            package, user=request.user,
            sold_price=request.POST.get("sold_price") or None,
            buyer_name=request.POST.get("buyer_name", "").strip(),
        )
    except (QuantityError, ValueError) as exc:
        error = str(exc)
    package.refresh_from_db()
    response = _package_panel(request, game, package, error=error)
    if not error:
        response["HX-Trigger"] = "qty-changed"
    return response


def card_search_fragment(request, game, pk):
    """Search-and-add rows for a package (reuses the quick-add query)."""
    game = get_object_or_404(Game, code=game)
    package = _get_package(game, pk)
    q = request.GET.get("q", "").strip()
    results = []
    if len(q) >= 3:
        results = (
            Printing.objects.filter(
                Q(expansion__game=game), name_normalized__contains=normalize_name(q),
                holdings__quantity__gt=0,
            )
            .select_related("expansion")
            .annotate(qty=Sum("holdings__quantity"))
            .order_by("slug")[:QUICK_ADD_LIMIT]
        )
    return render(request, "collection/partials/package_search.html", {
        "game": game, "package": package, "q": q, "results": results,
        "too_short": 0 < len(q) < 3,
    })


# -- rule-based package building ----------------------------------------------

def _rule_from(request) -> dict:
    """Parse the rule form. A blank price field means "no bound", not zero."""
    def price(name):
        raw = request.POST.get(name, "").strip()
        return services._to_decimal(raw) if raw else None

    return {
        "basis": request.POST.get("basis", "trend"),
        "min_price": price("min_price"),
        "max_price": price("max_price"),
        "expansion_slug": request.POST.get("expansion", "").strip(),
        "archetype": request.POST.get("archetype", "").strip(),
        "keep_each": max(0, min(99, int(request.POST.get("keep_each", "0") or 0))),
    }


@require_POST
def package_rule_preview(request, game, pk):
    game = get_object_or_404(Game, code=game)
    package = _get_package(game, pk)
    try:
        rule = _rule_from(request)
        preview = services.preview_package_rule(package, user=request.user, **rule)
    except (QuantityError, ValueError) as exc:
        return render(request, "collection/partials/bulk_error.html", {"error": str(exc)})
    return render(request, "collection/partials/package_rule_confirm.html", {
        "game": game, "package": package, "preview": preview, "rule": rule,
        "basis": rule["basis"],
    })


@require_POST
def package_rule_apply(request, game, pk):
    game = get_object_or_404(Game, code=game)
    package = _get_package(game, pk)
    try:
        cards, copies = services.apply_package_rule(
            package, user=request.user, **_rule_from(request)
        )
    except (QuantityError, ValueError) as exc:
        return render(request, "collection/partials/package_panel.html",
                      _package_context(game, package, request.user) | {"error": str(exc)})
    context = _package_context(game, package, request.user)
    context |= {"added_cards": cards, "added_copies": copies}
    return render(request, "collection/partials/package_panel.html", context)


def package_archetypes(request, game, pk):
    """Archetypes you actually own in this game — the list is otherwise 400 long."""
    game = get_object_or_404(Game, code=game)
    names = (
        Printing.objects.filter(
            expansion__game=game, holdings__quantity__gt=0, holdings__owner=request.user,
        )
        .exclude(piece__archetype="")
        .values_list("piece__archetype", flat=True)
        .distinct()
        .order_by("piece__archetype")
    )
    return render(request, "collection/partials/archetype_options.html", {
        "archetypes": [n for n in names if n],
    })
