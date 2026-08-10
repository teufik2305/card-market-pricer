"""Pull card metadata + art URLs from the public catalogs.

    manage.py import_catalog                 # every configured game
    manage.py import_catalog --game yugioh   # just YuGiOh
    manage.py import_catalog --limit 200     # smoke test

Read-only against the providers, one HTTP request each, and it never modifies
holdings, prices, or printing↔piece links.
"""

from django.core.management.base import BaseCommand, CommandError

from catalog.catalogs import CatalogError, importer_for
from catalog.models import Game


class Command(BaseCommand):
    help = "Import card metadata and art URLs from YGOPRODeck / digimoncard.io."

    def add_arguments(self, parser):
        parser.add_argument("--game", default=None, help="Game code (default: all).")
        parser.add_argument("--limit", type=int, default=None,
                            help="Only import the first N cards (smoke test).")

    def handle(self, *args, **options):
        games = Game.objects.all().order_by("code")
        if options["game"]:
            games = games.filter(code=options["game"])
            if not games:
                raise CommandError(f"Unknown game: {options['game']!r}")

        failures = 0
        for game in games:
            importer = importer_for(game)
            if importer is None:
                self.stdout.write(self.style.WARNING(
                    f"{game.name}: no catalog provider configured — skipping. "
                    "Set one on the game record (built-in, or a custom JSON API)."
                ))
                continue
            self.stdout.write(self.style.HTTP_INFO(f"\n=== {game.name}"))
            try:
                result = importer(game, log=self.stdout.write, limit=options["limit"])
            except CatalogError as exc:
                failures += 1
                self.stdout.write(self.style.ERROR(f"{game.name}: {exc}"))
                continue
            if result.version:
                self.stdout.write(f"provider database version: {result.version}")
            for note in result.notes:
                self.stdout.write(self.style.WARNING(f"note: {note}"))
            self.stdout.write(self.style.SUCCESS(f"{game.name}: {result.summary()}"))

        if failures:
            raise CommandError(f"{failures} game(s) failed to import.")
