from django.conf import settings
from django.db import models
from django.db.models import Q

from core.models import TimeStamped


class ScrapeJob(TimeStamped):
    """One scraping run (runner lands in a later phase; the models anchor FKs
    and the per-item checkpointing that replaces the legacy JSON checkpoints)."""

    class Type(models.TextChoices):
        # Cardmarket, via the browser — slow, and subject to bot protection.
        DISCOVER_EXPANSIONS = "discover_expansions", "Scrape: discover expansions"
        DISCOVER_CARDS = "discover_cards", "Scrape: discover cards"
        REFRESH_PRICES = "refresh_prices", "Scrape: refresh prices"
        # Public APIs — fast, no browser, no bot protection.
        IMPORT_CATALOG = "import_catalog", "API: import card catalog"
        RESOLVE_PRINTINGS = "resolve_printings", "API: match cards to catalog"
        CACHE_ART = "cache_art", "API: download card art"
        API_PRICES = "api_prices", "API: fill missing prices"

    # Jobs that never open a browser: they can be run any time, and a block or
    # a Chrome misconfiguration cannot affect them.
    API_TYPES = frozenset({
        "import_catalog", "resolve_printings", "cache_art", "api_prices",
    })

    @property
    def uses_browser(self) -> bool:
        return self.job_type not in self.API_TYPES

    class Status(models.TextChoices):
        PENDING = "pending"
        BACKING_UP = "backing_up"
        RUNNING = "running"
        CANCEL_REQUESTED = "cancel_requested"
        COMPLETED = "completed"
        COMPLETED_WITH_ERRORS = "completed_with_errors"
        FAILED = "failed"
        CANCELLED = "cancelled"

    game = models.ForeignKey("catalog.Game", on_delete=models.PROTECT)
    job_type = models.CharField(max_length=25, choices=Type.choices)
    status = models.CharField(max_length=25, choices=Status.choices, default=Status.PENDING)
    params = models.JSONField(default=dict, blank=True)
    total_items = models.PositiveIntegerField(default=0)
    processed_items = models.PositiveIntegerField(default=0)
    failed_items = models.PositiveIntegerField(default=0)
    pid = models.IntegerField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    log_path = models.CharField(max_length=250, blank=True)
    error = models.TextField(blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT
    )
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # At most one job may be running at a time (one Selenium session).
            models.UniqueConstraint(
                fields=["status"],
                condition=Q(status="running"),
                name="uniq_single_running_job",
            )
        ]

    def __str__(self):
        return f"#{self.pk} {self.job_type} ({self.status})"


class ScrapeJobItem(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        DONE = "done"
        FAILED = "failed"
        SKIPPED = "skipped"

    job = models.ForeignKey(ScrapeJob, on_delete=models.CASCADE, related_name="items")
    card = models.ForeignKey("catalog.Printing", null=True, blank=True, on_delete=models.CASCADE)
    expansion = models.ForeignKey(
        "catalog.Expansion", null=True, blank=True, on_delete=models.CASCADE
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    processed_at = models.DateTimeField(null=True, blank=True)
    message = models.CharField(max_length=250, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "card"], name="uniq_jobitem_card",
                condition=Q(card__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["job", "expansion"], name="uniq_jobitem_expansion",
                condition=Q(expansion__isnull=False),
            ),
        ]

    def __str__(self):
        return f"job {self.job_id} item ({self.status})"


class ScraperSettings(models.Model):
    """Operator settings for the scraper, editable from the panel.

    A single row (pk=1). These live in the database rather than the environment
    because they are things you change while operating — where Chrome is, how
    politely to crawl — not deployment configuration.
    """

    chrome_binary = models.CharField(
        max_length=500, blank=True,
        help_text="Full path to the Chrome executable. Leave blank to use the "
                  "platform default from settings.",
    )
    challenge_timeout = models.PositiveIntegerField(
        default=180,
        help_text="Seconds to wait for a Cloudflare check to clear (you clicking "
                  "the checkbox counts) before stopping the run.",
    )
    challenge_attempts = models.PositiveIntegerField(
        default=4,
        help_text="How many times to back off and retry when a Cloudflare check "
                  "doesn't clear, before giving up on the run. Each attempt waits "
                  "longer and starts a fresh browser.",
    )
    min_item_delay = models.PositiveIntegerField(
        default=4,
        help_text="Shortest pause between Cardmarket page loads, in seconds. "
                  "Applies to the scraper jobs only.",
    )
    max_item_delay = models.PositiveIntegerField(
        default=9,
        help_text="Longest pause between Cardmarket page loads. Slower is safer — "
                  "bursts are what get an IP blocked.",
    )

    class Meta:
        verbose_name = "scraper settings"
        verbose_name_plural = "scraper settings"

    def __str__(self):
        return "Scraper settings"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        # Drop force_insert so a second ScraperSettings(...).create() updates the
        # one row instead of failing on the primary key — the singleton is a real
        # invariant, not just a convention callers are asked to respect.
        kwargs.pop("force_insert", None)
        if self.max_item_delay < self.min_item_delay:
            self.max_item_delay = self.min_item_delay
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "ScraperSettings":
        return cls.objects.get_or_create(pk=1)[0]

    @property
    def chrome_path(self) -> str:
        from django.conf import settings as django_settings
        return self.chrome_binary or django_settings.SCRAPE_CHROME_BINARY
