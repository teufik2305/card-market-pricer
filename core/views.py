from pathlib import Path

from django.conf import settings
from django.shortcuts import render

from catalog.models import Game, Printing
from collection.models import Holding, QuantityChange, SellPackage, SellPackageItem
from pricing.services import collection_totals


def dashboard(request):
    game_cards = []
    for game in Game.objects.all().order_by("code"):
        totals = collection_totals(game=game)
        owned_rows = Holding.objects.filter(
            quantity__gt=0, card__expansion__game=game
        ).count()
        game_cards.append({
            "game": game,
            "totals": totals,
            "owned_rows": owned_rows,
            # A strip of art from the most valuable cards — the dashboard should
            # look like a collection, not a spreadsheet.
            "highlights": (
                Printing.objects.filter(
                    expansion__game=game, holdings__quantity__gt=0, piece__isnull=False
                )
                .select_related("piece")
                .order_by("-current_price_trend")
                .distinct()[:12]
            ),
            "reserved": SellPackageItem.objects.filter(
                package__kind=SellPackage.Kind.KEEP, package__game=game
            ).exclude(package__status=SellPackage.Status.CANCELLED).count(),
            "packages": SellPackage.objects.filter(
                game=game, kind=SellPackage.Kind.SELL
            ).exclude(status=SellPackage.Status.CANCELLED).count(),
        })

    recent_changes = (
        QuantityChange.objects.select_related("card", "card__expansion", "card__piece", "user")[:12]
    )

    backups = sorted(Path(settings.BACKUP_DIR).glob("db-*.sqlite3"), reverse=True)
    last_backup = backups[0].name if backups else None

    from scraping import services as scrape_services

    return render(request, "dashboard.html", {
        "game_cards": game_cards,
        "recent_changes": recent_changes,
        "last_backup": last_backup,
        "backup_count": len(backups),
        "active_job": scrape_services.active_job(),
    })
