from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import CardPiece, CardPieceAlias, Expansion, Game, Printing


@admin.register(Game)
class GameAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "catalog_provider", "has_catalog", "cardmarket_segment"]
    list_filter = ["catalog_provider"]
    fieldsets = (
        (None, {"fields": ("code", "name", "cardmarket_segment", "sheets_spreadsheet_id")}),
        ("Card catalog", {
            "fields": ("catalog_provider", "catalog_config"),
            "description": (
                "Where card names, types and artwork come from. Yu-Gi-Oh! and Digimon "
                "have built-in providers. For any other game pick <b>Custom JSON API</b> "
                "and describe the endpoint below — no code changes needed.<br><br>"
                "<pre>{\n"
                '  "url": "https://api.example.com/cards",\n'
                '  "params": {"limit": 5000},\n'
                '  "results_path": "data",\n'
                '  "fields": {\n'
                '    "external_id": "id",\n'
                '    "name": "name",\n'
                '    "card_type": "type",\n'
                '    "colour": "color",\n'
                '    "text": "description",\n'
                '    "image_url": "images.large"\n'
                "  }\n"
                "}</pre>"
                "Dotted paths reach into nested objects (<code>images.large</code>, "
                "<code>faces.0.art</code>). Only <code>external_id</code> and "
                "<code>name</code> are required."
            ),
        }),
    )

    @admin.display(boolean=True, description="Catalog ready")
    def has_catalog(self, obj):
        return obj.has_catalog


@admin.register(Expansion)
class ExpansionAdmin(admin.ModelAdmin):
    list_display = ["slug", "game", "fully_scraped"]
    list_filter = ["game", "fully_scraped"]
    search_fields = ["slug"]


@admin.register(Printing)
class PrintingAdmin(admin.ModelAdmin):
    list_display = ["slug", "expansion", "resolution_status",
                    "current_price_trend", "prices_updated_at"]
    list_filter = ["expansion__game", "resolution_status"]
    search_fields = ["slug", "name_normalized"]
    raw_id_fields = ["expansion", "piece"]
    list_select_related = ["expansion", "piece"]
    readonly_fields = ["current_price_from", "current_price_trend", "current_price_30d",
                       "prices_updated_at"]
    actions = ["mark_ignored"]

    @admin.action(description="Mark selected printings as 'ignored' (not a game piece)")
    def mark_ignored(self, request, queryset):
        updated = queryset.update(piece=None, resolution_status=Printing.Resolution.IGNORED)
        self.message_user(request, f"{updated} printing(s) marked as ignored.")


@admin.register(CardPiece)
class CardPieceAdmin(admin.ModelAdmin):
    list_display = ["art", "name", "game", "external_id", "card_type", "archetype",
                    "ban_tcg", "ambiguous_normalized"]
    list_display_links = ["art", "name"]
    list_filter = ["game", "ban_tcg", "ambiguous_normalized", "card_type", "attribute", "colour"]
    search_fields = ["name", "external_id", "archetype", "normalized_name"]
    readonly_fields = ["catalog_updated_at", "normalized_name", "big_art"]

    @admin.display(description="")
    def art(self, obj):
        if not obj.has_art("small"):
            return ""
        url = reverse("card-art", args=[obj.pk, "small"])
        return format_html('<img src="{}" style="height:44px;border-radius:3px">', url)

    @admin.display(description="Art")
    def big_art(self, obj):
        if not obj.pk or not obj.has_art("full"):
            return "—"
        url = reverse("card-art", args=[obj.pk, "full"])
        return format_html('<img src="{}" style="width:220px;border-radius:8px">', url)


admin.site.register(CardPieceAlias)
