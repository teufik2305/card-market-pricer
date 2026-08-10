from django.contrib import admin

from .models import ImportRun


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = ["command", "game", "ok", "created_at"]
    readonly_fields = ["command", "game", "ok", "counts", "report", "created_at"]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
