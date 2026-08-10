import pytest

from collection.models import Holding, QuantityChange, SellPackage, SellPackageItem
from collection.services import (
    QuantityError,
    adjust_quantity,
    finalize_sale,
    reserved_quantity,
    set_quantity,
    undo_change,
)


@pytest.mark.django_db
class TestSetQuantity:
    def test_creates_holding_and_audit_row_together(self, printing, superuser):
        holding, change = set_quantity(printing, 3, user=superuser)
        assert holding.quantity == 3
        assert change.old_quantity == 0
        assert change.new_quantity == 3
        assert change.delta == 3
        assert change.user == superuser
        assert QuantityChange.objects.count() == 1

    def test_noop_writes_no_audit_row(self, printing, superuser):
        set_quantity(printing, 3, user=superuser)
        _, change = set_quantity(printing, 3, user=superuser)
        assert change is None
        assert QuantityChange.objects.count() == 1

    def test_negative_rejected(self, printing, superuser):
        with pytest.raises(QuantityError):
            set_quantity(printing, -1, user=superuser)
        assert Holding.objects.count() == 0

    def test_out_of_range_rejected(self, printing, superuser):
        with pytest.raises(QuantityError):
            set_quantity(printing, 1000, user=superuser)

    def test_zero_keeps_row(self, printing, superuser):
        set_quantity(printing, 2, user=superuser)
        holding, _ = set_quantity(printing, 0, user=superuser)
        assert holding.pk is not None
        assert Holding.objects.filter(pk=holding.pk).exists()


@pytest.mark.django_db
class TestAdjustQuantity:
    def test_increment(self, printing, superuser):
        adjust_quantity(printing, +1, user=superuser)
        holding, _ = adjust_quantity(printing, +1, user=superuser)
        assert holding.quantity == 2

    def test_decrement_clamps_at_zero(self, printing, superuser):
        holding, change = adjust_quantity(printing, -1, user=superuser)
        assert holding.quantity == 0
        assert change is None  # 0 -> 0 is a no-op, no audit noise


@pytest.mark.django_db
class TestUndo:
    def test_undo_restores_and_audits(self, printing, superuser):
        set_quantity(printing, 5, user=superuser)
        _, change = set_quantity(printing, 2, user=superuser)
        holding, undo = undo_change(change, user=superuser)
        assert holding.quantity == 5
        assert undo.reason == QuantityChange.Reason.UNDO
        assert QuantityChange.objects.count() == 3  # nothing deleted


@pytest.mark.django_db
class TestAppendOnly:
    def test_audit_rows_cannot_be_updated(self, printing, superuser):
        _, change = set_quantity(printing, 1, user=superuser)
        change.new_quantity = 99
        with pytest.raises(TypeError):
            change.save()

    def test_audit_rows_cannot_be_deleted(self, printing, superuser):
        _, change = set_quantity(printing, 1, user=superuser)
        with pytest.raises(TypeError):
            change.delete()


@pytest.mark.django_db
class TestKeepPackages:
    """Reserving cards is a package with kind=KEEP now — there is no separate
    keeper flag on the holding."""

    def _keep(self, printing, superuser, quantity=1):
        package = SellPackage.objects.create(
            name="Keep", game=printing.expansion.game, owner=superuser,
            kind=SellPackage.Kind.KEEP,
        )
        SellPackageItem.objects.create(package=package, card=printing, quantity=quantity)
        return package

    def test_reserved_quantity_reads_the_keep_package(self, printing, superuser):
        set_quantity(printing, 3, user=superuser)
        assert reserved_quantity(printing, user=superuser) == 0
        self._keep(printing, superuser, 2)
        assert reserved_quantity(printing, user=superuser) == 2

    def test_cancelling_a_keep_package_releases_the_reservation(self, printing, superuser):
        set_quantity(printing, 3, user=superuser)
        package = self._keep(printing, superuser, 2)
        package.status = SellPackage.Status.CANCELLED
        package.save(update_fields=["status"])
        assert reserved_quantity(printing, user=superuser) == 0

    def test_reserved_copies_are_not_sellable(self, printing, superuser):
        set_quantity(printing, 3, user=superuser)
        self._keep(printing, superuser, 2)
        lot = SellPackage.objects.create(
            name="Lot", game=printing.expansion.game, owner=superuser,
        )
        SellPackageItem.objects.create(package=lot, card=printing, quantity=2)
        with pytest.raises(QuantityError, match="only 1 sellable"):
            finalize_sale(lot, user=superuser)

    def test_a_keep_package_cannot_be_sold(self, printing, superuser):
        set_quantity(printing, 2, user=superuser)
        package = self._keep(printing, superuser, 1)
        with pytest.raises(QuantityError, match="not for sale"):
            finalize_sale(package, user=superuser)
