import json

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from catalog.models import Expansion, Game, Printing
from collection.models import Holding, QuantityChange, SellPackage, SellPackageItem
from core.models import ImportRun
from pricing.models import PriceSnapshot


def card(qty=0, pfrom=0.02, trend=0.0, avg=0.0):
    return {
        "quantity": qty,
        "price_from": pfrom,
        "price_trend": trend,
        "price_30_day_avg": avg,
    }


@pytest.fixture
def data_dir(tmp_path):
    ygo = tmp_path / "YuGiOh"
    ygo.mkdir()
    ledger = {
        "Singles": {
            "Soul-Fusion": {
                "Chaos-Dragon-Levianeer": card(qty=1, pfrom=2.99, trend=5.36, avg=5.71),
                "Agave-Dragon": card(qty=0, pfrom=0.02, trend=0.13, avg=0.14),
                "Some-Bulk-Card": card(qty=0),  # never scraped individually
            },
            "Starter-Deck-Codebreaker": {
                "Cyberse-Wizard": card(qty=2, pfrom=0.05, trend=0.12, avg=0.11),
            },
        }
    }
    (ygo / "cardlist.json").write_text(json.dumps(ledger))
    (ygo / "cards-to-keep.json").write_text(json.dumps({
        "Soul-Fusion": ["Chaos-Dragon-Levianeer"],
        "Soul-Fusion-Typo": ["Ghost-Card"],
    }))
    (ygo / "cardlist-finished-expansions.json").write_text(json.dumps(["Soul-Fusion"]))
    stale = json.loads(json.dumps(ledger))
    stale["Singles"]["Starter-Deck-Codebreaker"]["Cyberse-Wizard"]["quantity"] = 5
    (ygo / "cardlist_without_cards_to_keep.json").write_text(json.dumps(stale))
    return tmp_path


@pytest.mark.django_db
class TestImportLegacy:
    def test_requires_superuser(self, data_dir):
        with pytest.raises(CommandError, match="No superuser"):
            call_command("import_legacy", game="yugioh", data_dir=str(data_dir))

    def test_gate_fails_on_count_mismatch(self, data_dir, superuser):
        with pytest.raises(CommandError, match="count gate mismatch"):
            call_command("import_legacy", game="yugioh", data_dir=str(data_dir))
        # transaction rolled back — nothing persisted except the failure log
        assert Printing.objects.count() == 0
        assert ImportRun.objects.filter(ok=False).exists()

    def test_import_with_drift_flag(self, data_dir, superuser, capsys):
        call_command(
            "import_legacy", game="yugioh", data_dir=str(data_dir), allow_count_drift=True
        )
        assert Game.objects.filter(code="yugioh").exists()
        assert Expansion.objects.count() == 2
        assert Printing.objects.count() == 4

        # Holdings only for qty>0, audited, owned by the superuser
        assert Holding.objects.count() == 2
        assert QuantityChange.objects.count() == 2
        assert set(Holding.objects.values_list("owner__username", flat=True)) == {"boss"}

        # Snapshots only for individually-scraped rows (qty>0 or trend/avg > 0)
        assert PriceSnapshot.objects.count() == 3
        bulk = Printing.objects.get(slug="Some-Bulk-Card")
        assert bulk.snapshots.count() == 0
        assert bulk.current_price_from is not None  # denormalized price still set

        # Keep list: matched entry flagged, unmatched reported
        # The keep-list now seeds a kind=KEEP package instead of a holding flag.
        keep = SellPackage.objects.get(kind=SellPackage.Kind.KEEP)
        item = keep.items.get()
        assert item.card.slug == "Chaos-Dragon-Levianeer"
        assert item.quantity == 1
        out = capsys.readouterr().out
        assert "KEEP-LIST UNMATCHED: Soul-Fusion-Typo/Ghost-Card" in out

        # finished-expansions flag
        assert Expansion.objects.get(slug="Soul-Fusion").fully_scraped is True
        assert Expansion.objects.get(slug="Starter-Deck-Codebreaker").fully_scraped is False

        # stale-file reconciliation report
        assert "RECONCILE: 1 quantity differences" in out
        assert "Cyberse-Wizard: imported 2, stale file 5" in out

    def test_rerun_is_skipped_without_force(self, data_dir, superuser, capsys):
        call_command("import_legacy", game="yugioh", data_dir=str(data_dir),
                     allow_count_drift=True)
        call_command("import_legacy", game="yugioh", data_dir=str(data_dir),
                     allow_count_drift=True)
        assert "already imported successfully — skipping" in capsys.readouterr().out
        assert QuantityChange.objects.count() == 2
        assert ImportRun.objects.filter(ok=True).count() == 1

    def test_force_rerun_never_touches_existing_holdings(self, data_dir, superuser):
        from collection.services import set_quantity

        call_command("import_legacy", game="yugioh", data_dir=str(data_dir),
                     allow_count_drift=True)
        # User edits after the first import: sells a card, lowers a keeper flag.
        sold = Printing.objects.get(slug="Cyberse-Wizard")
        set_quantity(sold, 0, user=superuser)
        keeper_card = Printing.objects.get(slug="Chaos-Dragon-Levianeer")
        SellPackageItem.objects.filter(card=keeper_card).delete()

        call_command("import_legacy", game="yugioh", data_dir=str(data_dir),
                     allow_count_drift=True, force=True)

        assert Holding.objects.get(card=sold).quantity == 0  # NOT reverted to 2
        assert not SellPackageItem.objects.filter(card=keeper_card).exists()  # NOT re-added
        assert Printing.objects.count() == 4
        assert PriceSnapshot.objects.count() == 3  # no duplicate snapshots
