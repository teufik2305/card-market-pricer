"""Open the scraper's browser so you can clear Cloudflare once, by hand.

Cardmarket's bot protection mints a clearance cookie when a human passes the
check. The scraper's Chrome profile is persistent, so doing this once seeds
every later job — you don't have to be sitting there when a 9-hour price
refresh hits a challenge at 3am.

    manage.py warm_browser --game yugioh
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from catalog.models import Game
from scraping.browser import build_driver, challenge_state, home_url, profile_dir


class Command(BaseCommand):
    help = "Open the scraper's Chrome so you can pass Cloudflare's check by hand."

    def add_arguments(self, parser):
        parser.add_argument("--game", default=None,
                            help="Game code to warm up (default: every game).")
        parser.add_argument("--minutes", type=int, default=10,
                            help="How long to wait for you to clear the check.")

    def handle(self, *args, **options):
        games = Game.objects.all().order_by("code")
        if options["game"]:
            games = games.filter(code=options["game"])
            if not games:
                raise CommandError(f"Unknown game: {options['game']!r}")
        if not games:
            raise CommandError("No games configured.")

        self.stdout.write(f"Chrome profile: {profile_dir()}")
        driver = build_driver(headless=False)
        try:
            for game in games:
                url = home_url(game.cardmarket_segment)
                self.stdout.write(f"\nOpening {url}")
                driver.get(url)
                if self._wait(driver, options["minutes"] * 60):
                    self.stdout.write(self.style.SUCCESS(f"  {game.name}: clear"))
                else:
                    self.stdout.write(self.style.ERROR(
                        f"  {game.name}: still blocked. If you never saw a checkbox this is "
                        "a hard block — wait 15–60 minutes and try again."
                    ))
                    return
            # Cookies are written to the profile on a clean shutdown.
            self.stdout.write(
                "\nClearance stored in the profile. Scrape jobs will reuse it; it lasts "
                f"until Cardmarket expires it (challenge wait per job: "
                f"{settings.SCRAPE_CHALLENGE_TIMEOUT}s)."
            )
        finally:
            driver.quit()

    def _wait(self, driver, seconds: int) -> bool:
        deadline = time.time() + seconds
        announced = False
        while time.time() < deadline:
            state = challenge_state(driver)
            if state is None:
                time.sleep(3)  # let the cookie land before moving on
                return True
            if state == "hard":
                self.stdout.write("  Cloudflare is refusing outright (no checkbox to click).")
                return False
            if not announced:
                self.stdout.write(self.style.WARNING(
                    "  Cloudflare check showing — click 'Verify you are human' in the "
                    f"Chrome window. Waiting up to {seconds // 60} min."
                ))
                announced = True
            time.sleep(3)
        return False
