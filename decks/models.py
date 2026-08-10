from django.conf import settings
from django.db import models

from core.models import TimeStamped


class Deck(TimeStamped):
    class Tier(models.TextChoices):
        META = "meta"
        ROGUE = "rogue"
        CASUAL = "casual"

    class SourceType(models.TextChoices):
        IMPORTED = "imported"
        MANUAL = "manual"
        CURATED = "curated"

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    game = models.ForeignKey("catalog.Game", on_delete=models.PROTECT)
    name = models.CharField(max_length=200)
    archetype = models.CharField(max_length=100, blank=True, db_index=True)
    format = models.CharField(max_length=30, default="TCG")
    tier = models.CharField(max_length=10, choices=Tier.choices, default=Tier.CASUAL)
    source_type = models.CharField(
        max_length=10, choices=SourceType.choices, default=SourceType.MANUAL
    )
    source_url = models.URLField(blank=True)
    is_built = models.BooleanField(
        default=False, help_text="Physically assembled — reserves its copies in matching."
    )
    notes = models.TextField(blank=True)

    def __str__(self):
        return self.name


class DeckCard(models.Model):
    class Section(models.TextChoices):
        MAIN = "main"
        EXTRA = "extra"
        SIDE = "side"
        EGG = "egg"  # Digimon egg deck

    deck = models.ForeignKey(Deck, on_delete=models.CASCADE, related_name="cards")
    piece = models.ForeignKey("catalog.CardPiece", on_delete=models.PROTECT)
    count = models.PositiveSmallIntegerField()
    section = models.CharField(max_length=5, choices=Section.choices, default=Section.MAIN)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["deck", "piece", "section"], name="uniq_deckcard_deck_piece_section"
            )
        ]

    def __str__(self):
        return f"{self.count}× piece {self.piece_id} ({self.section})"
