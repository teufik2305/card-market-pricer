from django.contrib import admin

from .models import ScrapeJob, ScrapeJobItem, ScraperSettings


@admin.register(ScrapeJob)
class ScrapeJobAdmin(admin.ModelAdmin):
    list_display = ["id", "job_type", "game", "status", "processed_items",
                    "total_items", "failed_items", "created_at"]
    list_filter = ["job_type", "status", "game"]
    readonly_fields = ["pid", "heartbeat_at", "started_at", "finished_at"]


admin.site.register(ScrapeJobItem)


@admin.register(ScraperSettings)
class ScraperSettingsAdmin(admin.ModelAdmin):
    """Editable from the scrape panel too; here for completeness."""

    list_display = ["chrome_binary", "challenge_timeout", "min_item_delay", "max_item_delay"]

    def has_add_permission(self, request):
        # Singleton: there is exactly one row and it is created on demand.
        return not ScraperSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False
