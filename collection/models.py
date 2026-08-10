from django.conf import settings
from django.db import models

from core.models import AppendOnly, TimeStamped


class Holding(TimeStamped):
    """How many copies of one Printing the owner has. Never deleted — quantity
    goes to 0 and the row plus its audit trail remain."""

    class Condition(models.TextChoices):
        MINT = "MT"
        NEAR_MINT = "NM"
        EXCELLENT = "EX"
        GOOD = "GD"
        LIGHT_PLAYED = "LP"
        PLAYED = "PL"
        POOR = "PO"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="holdings",
    )
    card = models.ForeignKey("catalog.Printing", on_delete=models.PROTECT, related_name="holdings")
    quantity = models.PositiveIntegerField(default=0)
    condition = models.CharField(max_length=2, choices=Condition.choices, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["owner", "card"], name="uniq_holding_owner_card"),
        ]

    def __str__(self):
        return f"{self.card} ×{self.quantity}"


class QuantityChange(AppendOnly):
    """Append-only audit log. Written ONLY by collection.services in the same
    transaction as the holding update — ledger and audit can never diverge."""

    class Reason(models.TextChoices):
        IMPORT_LEGACY = "import_legacy"
        MANUAL_EDIT = "manual_edit"
        BULK_EDIT = "bulk_edit"
        SALE = "sale"
        TRADE = "trade"
        CORRECTION = "correction"
        UNDO = "undo"

    holding = models.ForeignKey(Holding, on_delete=models.PROTECT, related_name="changes")
    card = models.ForeignKey("catalog.Printing", on_delete=models.PROTECT, related_name="quantity_changes")
    old_quantity = models.PositiveIntegerField()
    new_quantity = models.PositiveIntegerField()
    delta = models.IntegerField()
    old_keeper_quantity = models.PositiveIntegerField(
        default=0,
        help_text="Keeper reservation before the change, so undo can restore a "
                  "reservation that was clamped away by a quantity drop.",
    )
    reason = models.CharField(max_length=20, choices=Reason.choices)
    note = models.CharField(max_length=250, blank=True)
    sell_package = models.ForeignKey(
        "collection.SellPackage", null=True, blank=True, on_delete=models.PROTECT
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    batch_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text="Groups the changes of one bulk action for single-click undo.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["card", "created_at"])]

    def __str__(self):
        return f"{self.card_id}: {self.old_quantity} → {self.new_quantity} ({self.reason})"


class SellPackage(TimeStamped):
    """A bundle of holdings, either to sell together or to hold back.

    The separate keep-list is gone: reserving cards is now just a package with
    ``kind=KEEP``, which reads the same way in the UI and keeps one mechanism
    instead of two. A keep package is never sold; its copies are subtracted
    from what a sell package is allowed to commit.
    """

    class Kind(models.TextChoices):
        SELL = "sell", "For sale"
        KEEP = "keep", "Keep — never sell"

    class Status(models.TextChoices):
        DRAFT = "draft"
        LISTED = "listed"
        SOLD = "sold"
        CANCELLED = "cancelled"

    kind = models.CharField(max_length=5, choices=Kind.choices, default=Kind.SELL, db_index=True)

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    game = models.ForeignKey(
        "catalog.Game", null=True, blank=True, on_delete=models.PROTECT,
        related_name="sell_packages",
    )
    name = models.CharField(max_length=200)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    buyer_name = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)
    asking_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sold_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    sold_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.status})"


class SellPackageItem(models.Model):
    package = models.ForeignKey(SellPackage, on_delete=models.CASCADE, related_name="items")
    card = models.ForeignKey("catalog.Printing", on_delete=models.PROTECT)
    quantity = models.PositiveIntegerField()
    unit_price_at_listing = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["package", "card"], name="uniq_package_card")
        ]

    def __str__(self):
        return f"{self.card_id} ×{self.quantity} in package {self.package_id}"
