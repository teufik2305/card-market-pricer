from django.db import models

from core.models import TimeStamped


class Game(TimeStamped):
    class CatalogProvider(models.TextChoices):
        NONE = "", "No card catalog"
        YGOPRODECK = "ygoprodeck", "YGOPRODeck (Yu-Gi-Oh!)"
        DIGIMONCARD = "digimoncard", "digimoncard.io (Digimon)"
        CUSTOM = "custom", "Custom JSON API (configured below)"

    code = models.SlugField(unique=True)  # "yugioh", "digimon"
    name = models.CharField(max_length=50)
    cardmarket_segment = models.CharField(
        max_length=50, help_text="URL path piece on cardmarket.com, e.g. 'YuGiOh'"
    )
    sheets_spreadsheet_id = models.CharField(max_length=100, blank=True)

    # Where card metadata and art come from. Keeping this on the Game — rather
    # than a dict keyed by game code in the importer — means adding a game never
    # requires editing code, and a game without a catalog is an ordinary state
    # rather than a crash.
    catalog_provider = models.CharField(
        max_length=20, choices=CatalogProvider.choices, blank=True,
        help_text="Which API supplies card names, types and artwork for this game.",
    )
    catalog_config = models.JSONField(
        default=dict, blank=True,
        help_text=(
            'For the "custom" provider: {"url": "...", "params": {}, '
            '"results_path": "data", "fields": {"external_id": "id", "name": "name", '
            '"image_url": "images.large"}, "image_url_template": '
            '"https://cdn.example/{external_id}.jpg"}. Dotted field paths reach '
            "into nested objects."
        ),
    )

    def __str__(self):
        return self.name

    @property
    def has_catalog(self) -> bool:
        if self.catalog_provider == self.CatalogProvider.CUSTOM:
            return bool(self.catalog_config.get("url"))
        return bool(self.catalog_provider)


class Expansion(TimeStamped):
    game = models.ForeignKey(Game, on_delete=models.PROTECT, related_name="expansions")
    slug = models.CharField(max_length=200)
    display_name = models.CharField(max_length=200)
    fully_scraped = models.BooleanField(default=False)
    last_discovered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["game", "slug"], name="uniq_expansion_game_slug")
        ]
        ordering = ["slug"]

    def __str__(self):
        return self.display_name


class CardPiece(TimeStamped):
    """The canonical game piece (name/passcode/set-number) a decklist references.

    Populated from external catalogs — YGOPRODeck `cardinfo.php` for YuGiOh,
    digimoncard.io `getAllCards.php` for Digimon. Printings link here via a
    nullable FK, so one piece gathers every Cardmarket printing of that card.

    The columns are the ones worth filtering and sorting on across both games;
    everything else the APIs return lands in `extra` rather than growing a
    column per game.
    """

    game = models.ForeignKey(Game, on_delete=models.PROTECT, related_name="pieces")
    external_id = models.CharField(max_length=30)  # YGO passcode / Digimon "BT5-007"
    konami_id = models.CharField(max_length=30, blank=True)
    name = models.CharField(max_length=200)
    normalized_name = models.CharField(max_length=200, db_index=True)

    # -- shared, filterable -----------------------------------------------
    card_type = models.CharField(max_length=60, blank=True, db_index=True)
    frame_type = models.CharField(max_length=40, blank=True)
    race = models.CharField(max_length=60, blank=True, db_index=True)      # YGO race / Digimon digi_type
    attribute = models.CharField(max_length=60, blank=True, db_index=True)  # YGO attribute / Digimon Vaccine…
    colour = models.CharField(max_length=30, blank=True, db_index=True)     # Digimon only
    archetype = models.CharField(max_length=100, blank=True, db_index=True)
    level = models.PositiveSmallIntegerField(null=True, blank=True)
    atk = models.IntegerField(null=True, blank=True)   # YGO ATK / Digimon DP
    defence = models.IntegerField(null=True, blank=True)
    play_cost = models.PositiveSmallIntegerField(null=True, blank=True)      # Digimon
    evolution_cost = models.PositiveSmallIntegerField(null=True, blank=True)  # Digimon
    text = models.TextField(blank=True)
    ban_tcg = models.CharField(max_length=20, blank=True)  # "", Banned, Limited, Semi-Limited
    ban_ocg = models.CharField(max_length=20, blank=True)
    is_extra_deck = models.BooleanField(default=False)
    extra = models.JSONField(default=dict, blank=True)

    # -- art ---------------------------------------------------------------
    # Remote originals. Both providers forbid hotlinking, so templates never
    # point at these — they go through the local cache view.
    image_url = models.URLField(blank=True, max_length=300)
    image_small_url = models.URLField(blank=True, max_length=300)
    image_cropped_url = models.URLField(blank=True, max_length=300)

    catalog_price_eur = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    catalog_updated_at = models.DateTimeField(null=True, blank=True)
    ambiguous_normalized = models.BooleanField(
        default=False, help_text="Another piece shares this normalized name; never auto-resolve."
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["game", "external_id"], name="uniq_piece_game_extid")
        ]
        ordering = ["name"]

    def __str__(self):
        return self.name

    def has_art(self, size: str = "small") -> bool:
        return bool(self.remote_image(size))

    def remote_image(self, size: str = "small") -> str:
        return {
            "small": self.image_small_url or self.image_url,
            "cropped": self.image_cropped_url or self.image_url,
            "full": self.image_url or self.image_small_url,
        }.get(size, "")


class CardPieceAlias(models.Model):
    """Alternate-art passcodes (YGOPRODeck card_images[].id) that .ydk files use."""

    piece = models.ForeignKey(CardPiece, on_delete=models.CASCADE, related_name="aliases")
    alias_passcode = models.CharField(max_length=30, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["piece", "alias_passcode"], name="uniq_alias_piece_passcode"
            )
        ]

    def __str__(self):
        return f"{self.alias_passcode} → {self.piece_id}"


class Printing(TimeStamped):
    """One Cardmarket product page: expansion + slug (incl. -V2/rarity variants).

    Holdings and price snapshots hang off Printings; decks reference CardPieces.
    """

    class Resolution(models.TextChoices):
        UNRESOLVED = "unresolved"
        AUTO_EXACT = "auto_exact"
        AUTO_FUZZY = "auto_fuzzy"
        MANUAL = "manual"
        NO_MATCH = "no_match"
        IGNORED = "ignored"  # tokens, oversized promos — not game pieces

    expansion = models.ForeignKey(Expansion, on_delete=models.PROTECT, related_name="printings")
    slug = models.CharField(max_length=250)
    display_name = models.CharField(max_length=250)
    name_normalized = models.CharField(max_length=250, db_index=True)
    piece = models.ForeignKey(
        CardPiece, null=True, blank=True, on_delete=models.SET_NULL, related_name="printings"
    )
    resolution_status = models.CharField(
        max_length=12, choices=Resolution.choices, default=Resolution.UNRESOLVED
    )
    resolution_confidence = models.FloatField(null=True, blank=True)

    class PriceSource(models.TextChoices):
        SCRAPE = "scrape", "Cardmarket (scraped)"
        CATALOG = "catalog", "Catalog API (per-card estimate)"

    # Denormalized latest prices; history lives in pricing.PriceSnapshot.
    # price_source keeps the two honest apart: a scraped price is for THIS
    # printing, a catalog price is the card's average across every printing.
    price_source = models.CharField(
        max_length=10, choices=PriceSource.choices, blank=True, db_index=True
    )
    current_price_from = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    current_price_trend = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    current_price_30d = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    prices_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["expansion", "slug"], name="uniq_printing_expansion_slug")
        ]
        ordering = ["slug"]

    def __str__(self):
        return self.display_name

    @property
    def game(self):
        return self.expansion.game

    @property
    def cardmarket_url(self) -> str:
        segment = self.expansion.game.cardmarket_segment
        return (
            f"https://www.cardmarket.com/en/{segment}/Products/Singles/"
            f"{self.expansion.slug}/{self.slug}"
        )
