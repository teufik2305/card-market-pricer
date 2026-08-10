"""Link Cardmarket printings to catalog card pieces.

    manage.py resolve_printings                # unresolved rows, every game
    manage.py resolve_printings --game yugioh
    manage.py resolve_printings --redo         # retry previous no-matches too
    manage.py resolve_printings --no-fuzzy     # exact attempts only

Never touches printings a human resolved by hand.
"""

from django.core.management.base import BaseCommand, CommandError

from catalog.models import Game, Printing
from catalog.resolution import game_stats, resolve_game


class Command(BaseCommand):
    help = "Resolve Cardmarket printings to canonical CardPieces."

    def add_arguments(self, parser):
        parser.add_argument("--game", default=None)
        parser.add_argument("--redo", action="store_true",
                            help="Also retry printings previously marked no_match.")
        parser.add_argument("--no-fuzzy", action="store_true",
                            help="Exact attempts only; no fuzzy matching at all.")

    def handle(self, *args, **options):
        games = Game.objects.all().order_by("code")
        if options["game"]:
            games = games.filter(code=options["game"])
            if not games:
                raise CommandError(f"Unknown game: {options['game']!r}")

        for game in games:
            if not game.pieces.exists():
                self.stdout.write(self.style.WARNING(
                    f"{game.name}: no catalog imported yet — run import_catalog first."
                ))
                continue
            self.stdout.write(self.style.HTTP_INFO(f"\n=== {game.name}"))
            report = resolve_game(game, log=self.stdout.write, fuzzy=not options["no_fuzzy"],
                                  redo=options["redo"])
            self.stdout.write(f"{report.resolved} newly linked out of {report.total} examined")
            stats = game_stats(game)
            style = self.style.SUCCESS if stats["rate"] >= 96 else self.style.WARNING
            self.stdout.write(style(
                f"{game.name} overall: {stats['linked']:,}/{stats['eligible']:,} linked "
                f"({stats['rate']:.1f}%), {stats['unmatched']:,} unmatched"
            ))

            unresolved = Printing.objects.filter(
                expansion__game=game, resolution_status=Printing.Resolution.NO_MATCH,
                holdings__quantity__gt=0,
            ).distinct().count()
            if unresolved:
                self.stdout.write(
                    f"{unresolved} unmatched printings that you actually own — "
                    "these are the ones worth reviewing."
                )
