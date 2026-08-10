from django.db import models

from core.models import AppendOnly


class PriceSnapshot(AppendOnly):
    """Append-only price history. The legacy system overwrote a single mutable
    price in place; every observation is kept here with its timestamp."""

    class Source(models.TextChoices):
        LEGACY_IMPORT = "legacy_import"
        CARD_PAGE = "card_page"
        EXPANSION_LIST = "expansion_list"
        CATALOG_API = "catalog_api"  # YGOPRODeck / digimoncard.io, not Cardmarket

    card = models.ForeignKey("catalog.Printing", on_delete=models.PROTECT, related_name="snapshots")
    price_from = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_trend = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_30d_avg = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="EUR")
    observed_at = models.DateTimeField()
    source = models.CharField(max_length=20, choices=Source.choices)
    # PROTECT (not SET_NULL): the deletion collector's SET_NULL is a raw
    # queryset UPDATE that would silently mutate append-only history rows.
    scrape_job = models.ForeignKey(
        "scraping.ScrapeJob", null=True, blank=True, on_delete=models.PROTECT
    )

    class Meta:
        indexes = [models.Index(fields=["card", "-observed_at"])]
        ordering = ["-observed_at"]

    def __str__(self):
        return f"{self.card_id} @ {self.observed_at:%Y-%m-%d} ({self.source})"
