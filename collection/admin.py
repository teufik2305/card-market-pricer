from django.contrib import admin

from .models import Holding, QuantityChange, SellPackage, SellPackageItem


@admin.register(Holding)
class HoldingAdmin(admin.ModelAdmin):
    """Read-only: quantity mutations must go through collection.services so the
    audit trail stays complete. The admin is for inspection only."""

    list_display = ["card", "quantity", "owner", "updated_at"]
    search_fields = ["card__slug"]
    raw_id_fields = ["card", "owner"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(QuantityChange)
class QuantityChangeAdmin(admin.ModelAdmin):
    list_display = ["card", "old_quantity", "new_quantity", "reason", "user", "created_at"]
    list_filter = ["reason"]
    search_fields = ["card__slug"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class SellPackageItemInline(admin.TabularInline):
    model = SellPackageItem
    raw_id_fields = ["card"]
    extra = 0


@admin.register(SellPackage)
class SellPackageAdmin(admin.ModelAdmin):
    list_display = ["name", "status", "asking_price", "sold_price", "sold_at"]
    inlines = [SellPackageItemInline]
