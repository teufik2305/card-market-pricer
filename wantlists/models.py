from django.conf import settings
from django.db import models

from core.models import TimeStamped


class WantList(TimeStamped):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    game = models.ForeignKey("catalog.Game", on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    source_text = models.TextField(blank=True, help_text="Raw paste, kept for audit.")

    def __str__(self):
        return self.name


class WantListItem(models.Model):
    class MatchMethod(models.TextChoices):
        EXACT = "exact"
        NORMALIZED = "normalized"
        FUZZY_CONFIRMED = "fuzzy_confirmed"
        MANUAL = "manual"
        UNMATCHED = "unmatched"

    want_list = models.ForeignKey(WantList, on_delete=models.CASCADE, related_name="items")
    raw_line = models.CharField(max_length=250)
    requested_name = models.CharField(max_length=250)
    quantity = models.PositiveIntegerField(default=1)
    matched_name_normalized = models.CharField(max_length=250, blank=True)
    match_method = models.CharField(
        max_length=20, choices=MatchMethod.choices, default=MatchMethod.UNMATCHED
    )
    match_confidence = models.FloatField(null=True, blank=True)

    def __str__(self):
        return f"{self.quantity}× {self.requested_name} ({self.match_method})"
