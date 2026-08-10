from django.contrib import admin

from .models import PriceSnapshot


@admin.register(PriceSnapshot)
class PriceSnapshotAdmin(admin.ModelAdmin):
    list_display = ["card", "price_from", "price_trend", "price_30d_avg",
                    "currency", "source", "observed_at"]
    list_filter = ["source", "currency"]
    search_fields = ["card__slug"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
