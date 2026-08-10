"""One-shot, idempotent import of the legacy JSON ledgers into the database.

Reads (and never writes) data/{Segment}/cardlist.json, cards-to-keep.json and
cardlist-finished-expansions.json. Each game imports inside one transaction
with a hard count-validation gate — a failed gate rolls the whole game back.
"""

import json
from datetime import datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import Expansion, Game, Printing
from catalog.normalize import normalize_name, slug_display_name
from collection.models import Holding, QuantityChange, SellPackage, SellPackageItem
from collection.services import set_quantity
from core.models import ImportRun
from pricing.models import PriceSnapshot

GAMES = {
    "yugioh": {"name": "Yu-Gi-Oh!", "segment": "YuGiOh",
               "sheets_id": "12uBXds7mUM1Bk8O5cauwpHijLh3gY5CWvcMthWEXsmI"},
    "digimon": {"name": "Digimon", "segment": "Digimon",
                "sheets_id": "1LADI_rQx75tLAHUyxtvrG8-riaxdKZYjvhH5S0R-NaY"},
}

# Ground truth measured from the JSON ledgers at planning time. The import is
# rolled back if the files no longer produce exactly these numbers
# (--allow-count-drift downgrades the gate to a warning).
EXPECTED = {
    "yugioh": {"expansions": 1131, "printings": 60982, "holdings": 2190,
               "qty_sum": 3502, "keep_entries": 120},
    "digimon": {"expansions": 84, "printings": 5450, "holdings": 930,
                "qty_sum": 2040, "keep_entries": 0},
}


def _dec(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


class Command(BaseCommand):
    help = "Import the legacy JSON ledgers (read-only source) into the database."

    def add_arguments(self, parser):
        parser.add_argument("--game", choices=[*GAMES, "all"], default="all")
        parser.add_argument("--data-dir", default=None)
        parser.add_argument("--allow-count-drift", action="store_true")
        parser.add_argument(
            "--force", action="store_true",
            help="Re-run for a game that already imported successfully. Existing "
                 "holdings and keeper flags are never touched, even with --force.",
        )

    def handle(self, *args, **opts):
        data_dir = Path(opts["data_dir"] or settings.LEGACY_DATA_DIR)
        codes = list(GAMES) if opts["game"] == "all" else [opts["game"]]
        owner = get_user_model().objects.filter(is_superuser=True).order_by("pk").first()
        if owner is None:
            raise CommandError(
                "No superuser exists — run `manage.py createsuperuser` first so "
                "holdings get an owner."
            )

        for code in codes:
            if not opts["force"] and ImportRun.objects.filter(
                command="import_legacy", game__code=code, ok=True
            ).exists():
                self.stdout.write(self.style.WARNING(
                    f"[{code}] already imported successfully — skipping "
                    "(use --force to re-run; existing holdings are never modified)."
                ))
                continue
            report: list[str] = []
            counts: dict = {}
            try:
                with transaction.atomic():
                    counts = self._import_game(code, data_dir, owner, report)
                    self._validate(code, counts, opts["allow_count_drift"], report)
            except Exception as exc:
                ImportRun.objects.create(
                    command="import_legacy", ok=False,
                    counts=counts, report="\n".join([*report, f"FAILED: {exc}"]),
                )
                raise
            game = Game.objects.get(code=code)
            ImportRun.objects.create(
                command="import_legacy", game=game, ok=True,
                counts=counts, report="\n".join(report),
            )
            self.stdout.write("\n".join(report))
            self.stdout.write(self.style.SUCCESS(f"[{code}] import OK: {counts}"))

    # -- per-game import ----------------------------------------------------

    def _import_game(self, code: str, data_dir: Path, owner, report: list[str]) -> dict:
        cfg = GAMES[code]
        game_dir = data_dir / cfg["segment"]
        cardlist_path = game_dir / "cardlist.json"
        if not cardlist_path.exists():
            raise CommandError(f"{cardlist_path} not found")

        with cardlist_path.open() as fh:
            ledger = json.load(fh)["Singles"]
        observed_at = datetime.fromtimestamp(cardlist_path.stat().st_mtime, tz=dt_timezone.utc)
        report.append(f"[{code}] source {cardlist_path} (mtime {observed_at:%Y-%m-%d})")

        game, _ = Game.objects.update_or_create(
            code=code,
            defaults={"name": cfg["name"], "cardmarket_segment": cfg["segment"],
                      "sheets_spreadsheet_id": cfg["sheets_id"]},
        )

        # Expansions
        existing_exp = {e.slug: e for e in game.expansions.all()}
        new_expansions = [
            Expansion(game=game, slug=slug, display_name=slug_display_name(slug))
            for slug in ledger
            if slug not in existing_exp
        ]
        Expansion.objects.bulk_create(new_expansions, batch_size=2000)
        expansions = {e.slug: e for e in game.expansions.all()}

        # Printings (bulk; prices denormalized at creation only — snapshots own
        # them afterwards)
        existing_printings = set(
            Printing.objects.filter(expansion__game=game)
            .values_list("expansion__slug", "slug")
        )
        to_create = []
        for exp_slug, cards in ledger.items():
            expansion = expansions[exp_slug]
            for card_slug, info in cards.items():
                if (exp_slug, card_slug) in existing_printings:
                    continue
                to_create.append(Printing(
                    expansion=expansion,
                    slug=card_slug,
                    display_name=slug_display_name(card_slug),
                    name_normalized=normalize_name(card_slug),
                    current_price_from=_dec(info["price_from"]),
                    current_price_trend=_dec(info["price_trend"]),
                    current_price_30d=_dec(info["price_30_day_avg"]),
                    prices_updated_at=observed_at,
                ))
        Printing.objects.bulk_create(to_create, batch_size=2000)
        printing_ids = {
            (exp_slug, slug): pk
            for pk, exp_slug, slug in Printing.objects.filter(expansion__game=game)
            .values_list("pk", "expansion__slug", "slug")
        }
        report.append(f"[{code}] {len(new_expansions)} new expansions, "
                      f"{len(to_create)} new printings")

        # Initial price snapshots — only rows that were actually scraped
        # individually (owned, or with a non-zero trend/30d price).
        already_snapshotted = set(
            PriceSnapshot.objects.filter(
                card__expansion__game=game, source=PriceSnapshot.Source.LEGACY_IMPORT
            ).values_list("card_id", flat=True)
        )
        snapshots = []
        for exp_slug, cards in ledger.items():
            for card_slug, info in cards.items():
                if not (info["quantity"] > 0 or info["price_trend"] > 0
                        or info["price_30_day_avg"] > 0):
                    continue
                card_id = printing_ids[(exp_slug, card_slug)]
                if card_id in already_snapshotted:
                    continue
                snapshots.append(PriceSnapshot(
                    card_id=card_id,
                    price_from=_dec(info["price_from"]),
                    price_trend=_dec(info["price_trend"]),
                    price_30d_avg=_dec(info["price_30_day_avg"]),
                    observed_at=observed_at,
                    source=PriceSnapshot.Source.LEGACY_IMPORT,
                ))
        PriceSnapshot.objects.bulk_create(snapshots, batch_size=2000)
        report.append(f"[{code}] {len(snapshots)} legacy price snapshots")

        # Holdings — through the audited service, the only quantity write path.
        # Seed-only: a card that already has ANY holding row is never touched,
        # so re-running the import can never revert quantity edits made in the
        # app since the first import.
        owned_keys = [
            (exp_slug, card_slug)
            for exp_slug, cards in ledger.items()
            for card_slug, info in cards.items()
            if info["quantity"] > 0
        ]
        owned_ids = [printing_ids[key] for key in owned_keys]
        printings_by_id = Printing.objects.in_bulk(owned_ids)
        already_held = set(
            Holding.objects.filter(card_id__in=owned_ids).values_list("card_id", flat=True)
        )
        holdings = 0
        qty_sum = 0
        seeded = 0
        for (exp_slug, card_slug), card_id in zip(owned_keys, owned_ids, strict=True):
            qty = ledger[exp_slug][card_slug]["quantity"]
            # Gate counters describe what the FILES say, independent of DB state.
            holdings += 1
            qty_sum += qty
            if card_id in already_held:
                continue
            set_quantity(printings_by_id[card_id], qty, user=owner,
                         reason=QuantityChange.Reason.IMPORT_LEGACY,
                         note="legacy cardlist.json import")
            seeded += 1
        if seeded < holdings:
            report.append(
                f"[{code}] {holdings - seeded} holdings already existed and were "
                "left untouched (seed-only import)"
            )

        seeded_ids = {cid for cid in owned_ids if cid not in already_held}
        keep_entries = self._import_keep_list(code, game_dir, printing_ids, seeded_ids, report)
        self._import_finished_expansions(game_dir, expansions, report)
        if code == "yugioh":
            self._report_stale_file_diffs(game_dir, ledger, report)

        # All gate counts describe the FILES, so the gate stays meaningful
        # regardless of what already existed in the database.
        return {
            "expansions": len(ledger),
            "printings": sum(len(cards) for cards in ledger.values()),
            "holdings": holdings,
            "qty_sum": qty_sum,
            "keep_entries": keep_entries,
        }

    def _import_keep_list(self, code, game_dir, printing_ids, seeded_ids, report) -> int:
        """Seed the game's keep package from cards-to-keep.json.

        The standalone keeper flag is gone; reserved cards are now items in a
        package with kind=KEEP. Only holdings seeded by THIS run are added, so a
        re-run can never clobber reservations the user changed in the app.
        """
        keep_path = game_dir / "cards-to-keep.json"
        if not keep_path.exists():
            return 0

        with keep_path.open() as fh:
            keep = json.load(fh)
        matched = 0
        flagged = 0
        package = None
        for exp_slug, card_slugs in keep.items():
            for card_slug in card_slugs:
                card_id = printing_ids.get((exp_slug, card_slug))
                if card_id is None:
                    report.append(f"[{code}] KEEP-LIST UNMATCHED: {exp_slug}/{card_slug}")
                    continue
                matched += 1
                if card_id not in seeded_ids:
                    holding = Holding.objects.filter(card_id=card_id, quantity__gt=0).first()
                    if holding is None:
                        report.append(
                            f"[{code}] keep-list entry not owned (qty 0): {exp_slug}/{card_slug}"
                        )
                    continue
                holding = Holding.objects.get(card_id=card_id)
                if package is None:
                    package = self._keep_package(holding)
                SellPackageItem.objects.update_or_create(
                    package=package, card_id=card_id,
                    defaults={"quantity": holding.quantity, "unit_price_at_listing": None},
                )
                flagged += 1
        report.append(f"[{code}] keep-list: {matched} entries matched, "
                      f"{flagged} cards reserved in the keep package")
        return matched

    @staticmethod
    def _keep_package(holding):
        game = holding.card.expansion.game
        package, _ = SellPackage.objects.get_or_create(
            owner=holding.owner, game=game, kind=SellPackage.Kind.KEEP,
            name=f"Keep — {game.name}",
            defaults={"notes": "Seeded from the legacy cards-to-keep.json."},
        )
        return package

    def _import_finished_expansions(self, game_dir, expansions, report):
        path = game_dir / "cardlist-finished-expansions.json"
        if not path.exists():
            return
        with path.open() as fh:
            finished = json.load(fh)
        unmatched = [slug for slug in finished if slug not in expansions]
        matched_ids = [expansions[slug].pk for slug in finished if slug in expansions]
        Expansion.objects.filter(pk__in=matched_ids).update(fully_scraped=True)
        report.append(f"[…] finished-expansions: {len(matched_ids)} flagged fully_scraped")
        for slug in unmatched:
            report.append(f"[…] FINISHED-EXPANSION UNMATCHED: {slug}")

    def _report_stale_file_diffs(self, game_dir, ledger, report):
        """The misnamed cardlist_without_cards_to_keep.json is a stale snapshot
        that drifted from the main ledger. Nothing is imported from it; the
        quantity differences are listed for manual reconciliation."""
        stale_path = game_dir / "cardlist_without_cards_to_keep.json"
        if not stale_path.exists():
            return
        with stale_path.open() as fh:
            stale = json.load(fh)["Singles"]
        diffs = []
        for exp_slug, cards in ledger.items():
            for card_slug, info in cards.items():
                stale_qty = stale.get(exp_slug, {}).get(card_slug, {}).get("quantity")
                if stale_qty is not None and stale_qty != info["quantity"]:
                    diffs.append((exp_slug, card_slug, info["quantity"], stale_qty))
        # Both directions: cards that exist ONLY in the stale file matter too.
        only_stale = [
            (exp_slug, card_slug, info.get("quantity", 0))
            for exp_slug, cards in stale.items()
            for card_slug, info in cards.items()
            if card_slug not in ledger.get(exp_slug, {})
        ]
        report.append(
            f"[yugioh] RECONCILE: {len(diffs)} quantity differences vs the stale "
            "cardlist_without_cards_to_keep.json (imported value ← cardlist.json):"
        )
        for exp_slug, card_slug, main_qty, stale_qty in diffs:
            report.append(f"    {exp_slug}/{card_slug}: imported {main_qty}, stale file {stale_qty}")
        if only_stale:
            report.append(
                f"[yugioh] RECONCILE: {len(only_stale)} cards exist only in the stale file "
                "(not imported):"
            )
            for exp_slug, card_slug, qty in only_stale:
                report.append(f"    {exp_slug}/{card_slug} (stale qty {qty})")

    def _validate(self, code, counts, allow_drift, report):
        expected = EXPECTED[code]
        mismatches = {
            key: (counts.get(key), want)
            for key, want in expected.items()
            if counts.get(key) != want
        }
        if not mismatches:
            report.append(f"[{code}] validation gate PASSED: {expected}")
            return
        msg = f"[{code}] count gate mismatch (got, expected): {mismatches}"
        if allow_drift:
            report.append(f"WARNING {msg}")
        else:
            raise CommandError(msg + " — rerun with --allow-count-drift to accept")
