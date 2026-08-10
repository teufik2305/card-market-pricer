"""Cloudflare handling: detect the challenge, wait for a human, stop the run.

Job #7 (2026-08-08) burned 13 expansions against Cloudflare's interstitial and
reported them as scraper failures. These tests pin the behavior that replaces
that: a challenge is recognised as a challenge, a visible browser gets a chance
to clear it, and an unclearable block aborts the whole job instead of marking
hundreds of sets failed.
"""

from decimal import Decimal

import pytest
from selenium.common.exceptions import NoSuchElementException

from catalog.models import Expansion, Printing
from scraping import browser, runner, services
from scraping.errors import ScrapeBlocked
from scraping.models import ScrapeJob, ScrapeJobItem
from scraping.scrapers import cards as cards_module
from scraping.scrapers.cards import discover_cards

REAL_TITLE = "Soul Fusion - YGO Singles | Cardmarket"
SOFT_TITLE = "Just a moment..."
HARD_TITLE = "Attention Required! | Cloudflare"


class Element:
    def __init__(self, text=""):
        self.text = text

    def click(self):
        pass


class StubDriver:
    """Enough of a WebDriver for navigation and the listing scraper."""

    def __init__(self, titles=(REAL_TITLE,), elements=None):
        self._titles = list(titles)
        self.title = self._titles.pop(0)
        self.elements = elements or {}
        self.visited = []
        self.quit_calls = 0

    def advance(self):
        if self._titles:
            self.title = self._titles.pop(0)

    def get(self, url):
        self.visited.append(url)

    def find_elements(self, how, what):
        return self.elements.get(what, [])

    def find_element(self, how, what):
        found = self.elements.get(what)
        if not found:
            raise NoSuchElementException(what)
        return found[0]

    def execute_script(self, *args, **kwargs):
        return None

    def quit(self):
        self.quit_calls += 1


class FakeTime:
    """A clock the test drives, so waiting costs no wall time."""

    def __init__(self, on_sleep=None):
        self.now = 0.0
        self.on_sleep = on_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(browser, "random_delay", lambda *a, **kw: None)
    return lambda *a, **kw: None  # a no-op log


class TestChallengeDetection:
    def test_interstitial_is_soft(self):
        assert browser.challenge_state(StubDriver([SOFT_TITLE])) == "soft"

    def test_refusal_is_hard(self):
        assert browser.challenge_state(StubDriver([HARD_TITLE])) == "hard"

    def test_widget_without_a_telltale_title_is_soft(self):
        driver = StubDriver([""], {"#challenge-running": [Element()]})
        assert browser.challenge_state(driver) == "soft"

    def test_error_code_block_is_hard(self):
        driver = StubDriver(["cardmarket"], {".cf-error-code": [Element("1015")]})
        assert browser.challenge_state(driver) == "hard"

    def test_real_page_is_clear(self):
        assert browser.challenge_state(StubDriver([REAL_TITLE])) is None


class TestClearance:
    def test_hard_block_raises_immediately_with_advice(self, quiet, monkeypatch):
        clock = FakeTime()
        monkeypatch.setattr(browser, "time", clock)
        with pytest.raises(ScrapeBlocked) as exc:
            browser.navigate(StubDriver([HARD_TITLE]), "https://x/y", log=quiet)
        assert "Nothing was marked as scraped" in str(exc.value)
        assert "Verify you are human" in str(exc.value)
        assert clock.now == 0  # did not sit there waiting

    def test_waits_for_the_human_to_solve_it(self, quiet, monkeypatch):
        driver = StubDriver([SOFT_TITLE, SOFT_TITLE, SOFT_TITLE, REAL_TITLE])
        monkeypatch.setattr(browser, "time", FakeTime(on_sleep=driver.advance))
        browser.navigate(driver, "https://x/y", log=quiet)  # returns = cleared
        assert driver.title == REAL_TITLE

    def test_gives_up_after_the_timeout(self, quiet, monkeypatch):
        driver = StubDriver([SOFT_TITLE])
        monkeypatch.setattr(browser, "time", FakeTime())
        with pytest.raises(ScrapeBlocked, match="did not clear within 30s"):
            browser.await_clearance(driver, "https://x/y", log=quiet, timeout=30)

    def test_soft_challenge_that_turns_into_a_block_stops_waiting(self, quiet, monkeypatch):
        driver = StubDriver([SOFT_TITLE, HARD_TITLE])
        monkeypatch.setattr(browser, "time", FakeTime(on_sleep=driver.advance))
        with pytest.raises(ScrapeBlocked, match="access refused"):
            browser.await_clearance(driver, "https://x/y", log=quiet, timeout=300)


class FakeProcess:
    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.terminated = True


class TestLaunchStrategy:
    """Measured 2026-08-09: chromedriver-launched Chrome is refused by
    Cardmarket while a normally-launched Chrome passes on the same IP and URL.
    So we start the browser and Selenium only attaches afterwards."""

    @pytest.fixture
    def launched(self, monkeypatch, tmp_path, quiet):
        captured = {"process": FakeProcess()}

        def fake_popen(command, **kwargs):
            captured["command"] = command
            return captured["process"]

        monkeypatch.setattr(browser.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(browser, "_cdp_title", lambda port: REAL_TITLE)

        def fake_chrome(options=None, **kwargs):
            captured["debugger"] = options.debugger_address
            captured["chrome_kwargs"] = kwargs
            return StubDriver()

        monkeypatch.setattr(browser.webdriver, "Chrome", fake_chrome)
        captured["driver"] = browser.build_driver(
            profile=tmp_path / "prof", warm_url="https://cardmarket.test/en/YuGiOh"
        )
        return captured

    def test_we_launch_chrome_ourselves_on_the_warm_up_page(self, launched):
        command = launched["command"]
        assert command[0] == browser.settings.SCRAPE_CHROME_BINARY
        assert any(arg.startswith("--remote-debugging-port=") for arg in command)
        # The warm page is loaded by Chrome alone — any challenge is met before
        # Selenium is anywhere near the session.
        assert command[-1] == "https://cardmarket.test/en/YuGiOh"

    def test_profile_is_persistent(self, launched, tmp_path):
        assert f"--user-data-dir={tmp_path / 'prof'}" in launched["command"]

    def test_no_automation_switches_reach_chrome(self, launched):
        command = " ".join(launched["command"])
        for switch in ("--enable-automation", "--no-sandbox", "--disable-gpu", "--test-type"):
            assert switch not in command

    def test_selenium_attaches_rather_than_launching(self, launched):
        assert launched["debugger"].startswith("127.0.0.1:")
        assert launched["chrome_kwargs"] == {}  # Selenium Manager finds the driver

    def test_quit_stops_the_browser_we_started(self, launched):
        launched["driver"].quit()
        assert launched["process"].terminated

    def test_a_block_on_the_warm_up_page_kills_the_browser(self, monkeypatch, tmp_path, quiet):
        process = FakeProcess()
        monkeypatch.setattr(browser.subprocess, "Popen", lambda command, **kw: process)
        monkeypatch.setattr(browser, "_cdp_title", lambda port: HARD_TITLE)
        monkeypatch.setattr(browser, "time", FakeTime())
        with pytest.raises(ScrapeBlocked, match="access refused"):
            browser.build_driver(profile=tmp_path / "prof", warm_url="https://x", log=quiet)
        assert process.terminated  # no orphaned Chrome left behind

    def test_profile_dir_defaults_under_var(self, settings, tmp_path):
        settings.SCRAPE_PROFILE_DIR = str(tmp_path / "chrome-profile")
        assert browser.profile_dir().is_dir()


class Tile:
    """A gallery tile as Cardmarket serves it since the 2026 redesign."""

    def __init__(self, href, price_text=None):
        self.href = href
        self.price_text = price_text

    def get_attribute(self, name):
        return self.href if name == "href" else None

    def find_element(self, how, what):
        if self.price_text is None:
            raise NoSuchElementException(what)
        return Element(self.price_text)


class TestGalleryMarkup:
    def _driver(self, tiles):
        return StubDriver([REAL_TITLE], {cards_module.TILE: tiles})

    def test_reads_slug_and_price_from_each_tile(self):
        driver = self._driver([
            Tile("https://www.cardmarket.com/en/YuGiOh/Products/Singles/"
                 "Duelist-Pack-Yusei-Japanese/Armory-Arm", "6,00 €"),
            Tile("https://www.cardmarket.com/en/YuGiOh/Products/Singles/"
                 "Duelist-Pack-Yusei-Japanese/Tuningware", "0,20 €"),
        ])
        rows = cards_module._read_rows(driver, "Duelist-Pack-Yusei-Japanese")
        assert rows == [("Armory-Arm", Decimal("6.00")), ("Tuningware", Decimal("0.20"))]

    def test_a_priceless_tile_does_not_shift_its_neighbours(self):
        """The notebook's zip-by-index bug in its new clothes: one tile without
        a price must cost that tile its price and nothing else."""
        driver = self._driver([
            Tile("https://x/en/YuGiOh/Products/Singles/Soul-Fusion/No-Sellers"),
            Tile("https://x/en/YuGiOh/Products/Singles/Soul-Fusion/Danger-Nessie", "1,50 €"),
        ])
        assert cards_module._read_rows(driver, "Soul-Fusion") == [
            ("No-Sellers", None), ("Danger-Nessie", Decimal("1.50")),
        ]

    def test_tiles_from_another_expansion_are_skipped(self):
        driver = self._driver([
            Tile("https://x/en/YuGiOh/Products/Singles/Soul-Fusion/Danger-Nessie", "1,50 €"),
            Tile("https://x/en/YuGiOh/Products/Singles/Other-Set/Cross-Link", "9,99 €"),
        ])
        assert cards_module._read_rows(driver, "Soul-Fusion") == [
            ("Danger-Nessie", Decimal("1.50")),
        ]

    def test_page_count_reads_the_new_pagination_text(self):
        driver = StubDriver([REAL_TITLE], {"pagination": [Element("Page 1 of 5")]})
        assert cards_module._page_count(driver) == 5

    def test_missing_pagination_still_fails_closed(self):
        with pytest.raises(cards_module.ScrapeError, match="pagination element not found"):
            cards_module._page_count(StubDriver([REAL_TITLE]))


class TestEmptyExpansion:
    def test_a_genuinely_empty_set_is_not_a_failure(self, quiet, monkeypatch):
        """Cardmarket lists expansions with no singles. The legacy notebook
        checked for that; without the check it looks identical to a bot block."""
        monkeypatch.setattr(browser, "time", FakeTime())
        driver = StubDriver([REAL_TITLE], {"noResults": [Element("No results found")]})
        cards, complete = discover_cards(driver, "YuGiOh", "Empty-Set", log=quiet)
        assert cards == {} and complete is True


@pytest.mark.django_db
class TestJobAborts:
    @pytest.fixture
    def two_expansions(self, game, expansion):
        Expansion.objects.create(
            game=game, slug="Second-Set", display_name="Second Set", fully_scraped=False
        )
        Expansion.objects.filter(pk=expansion.pk).update(fully_scraped=False)
        return Expansion.objects.filter(game=game)

    @pytest.fixture
    def no_backup(self, monkeypatch):
        monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)

    def test_block_stops_the_job_instead_of_failing_every_set(
        self, game, superuser, two_expansions, monkeypatch, no_backup
    ):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.DISCOVER_CARDS,
            params={"expansion": ""}, user=superuser,
        )
        assert job.total_items == 2
        monkeypatch.setattr(runner, "build_driver", lambda **kw: StubDriver())
        monkeypatch.setattr(runner, "random_delay", lambda *a, **kw: None)

        def blocked(*args, **kwargs):
            raise ScrapeBlocked("Cardmarket is blocking the scraper (access refused).")

        monkeypatch.setattr(runner, "discover_cards", blocked)
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.FAILED
        assert "blocking the scraper" in job.error
        assert "Traceback" not in job.error  # operational condition, not a crash
        assert job.failed_items == 0  # no set was blamed for Cloudflare
        # Terminal means terminal — nothing resumes, so unreached sets are
        # recorded as skipped rather than left looking like pending work.
        assert job.items.filter(status=ScrapeJobItem.Status.PENDING).count() == 0
        assert job.items.filter(status=ScrapeJobItem.Status.SKIPPED).count() == 2
        assert "bot protection" in job.items.first().message
        assert not Expansion.objects.filter(fully_scraped=True).exists()

    def test_one_browser_serves_the_whole_run(
        self, game, superuser, two_expansions, monkeypatch, no_backup
    ):
        """A fresh driver per expansion discards the Cloudflare clearance
        cookie and re-triggers the check on every set."""
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.DISCOVER_CARDS,
            params={"expansion": ""}, user=superuser,
        )
        built = []
        monkeypatch.setattr(runner, "build_driver", lambda **kw: built.append(StubDriver()) or built[-1])
        monkeypatch.setattr(runner, "random_delay", lambda *a, **kw: None)
        monkeypatch.setattr(
            runner, "discover_cards",
            lambda driver, seg, slug, log, deep_search: ({}, True),
        )
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.COMPLETED
        assert job.processed_items == 2
        assert len(built) == 1

    def test_price_refresh_does_not_retry_through_a_block(
        self, game, owned_printing, superuser, monkeypatch, no_backup
    ):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        calls = []
        monkeypatch.setattr(runner, "build_driver", lambda **kw: StubDriver())

        def blocked(*args, **kwargs):
            calls.append(1)
            raise ScrapeBlocked("Cardmarket is blocking the scraper (access refused).")

        monkeypatch.setattr(runner, "scrape_card_prices", blocked)
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.FAILED
        assert len(calls) == 1  # not 3 — retrying a block is pointless
        assert job.items.filter(status=ScrapeJobItem.Status.SKIPPED).count() == 1


@pytest.fixture
def owned_printing(printing, superuser):
    from collection.services import set_quantity
    set_quantity(printing, 2, user=superuser)
    return printing


@pytest.mark.django_db
class TestChallengeWaitGuards:
    """Job #36 died after 5 cards because the wait was set to 10s — shorter than
    the check takes to clear itself. The form allowed that without a word."""

    @pytest.fixture
    def staff(self, client, superuser):
        client.force_login(superuser)
        return client

    def test_a_wait_below_the_self_clear_window_is_refused(self, staff):
        from scraping.models import ScraperSettings
        response = staff.post("/scrape/settings/", {
            "chrome_binary": "", "challenge_timeout": "10",
            "min_item_delay": "4", "max_item_delay": "9",
        })
        assert "shorter than the check itself" in response.text
        assert ScraperSettings.load().challenge_timeout >= 30

    def test_a_sensible_wait_is_accepted(self, staff):
        from scraping.models import ScraperSettings
        response = staff.post("/scrape/settings/", {
            "chrome_binary": "", "challenge_timeout": "180",
            "min_item_delay": "4", "max_item_delay": "9",
        })
        assert response.status_code == 302
        assert ScraperSettings.load().challenge_timeout == 180

    def test_the_block_message_points_at_the_setting(self, quiet, monkeypatch):
        monkeypatch.setattr(browser, "time", FakeTime())
        with pytest.raises(ScrapeBlocked) as exc:
            browser.await_clearance(
                StubDriver([SOFT_TITLE]), "https://x/y", log=quiet, timeout=10
            )
        assert "Cloudflare wait" in str(exc.value)


class TestHumanAlert:
    def test_the_plain_interstitial_does_not_summon_anyone(self):
        """It clears on its own; beeping for it would train you to ignore beeps."""
        assert browser.needs_a_human(StubDriver([SOFT_TITLE])) is False

    def test_the_interactive_widget_does(self):
        driver = StubDriver([SOFT_TITLE], {".cf-turnstile": [Element()]})
        assert browser.needs_a_human(driver) is True

    def test_alerting_never_breaks_the_run(self, monkeypatch):
        """A failed notification must not take a 9-hour job down with it."""
        monkeypatch.setattr(browser.subprocess, "run",
                            lambda *a, **kw: (_ for _ in ()).throw(OSError("no osascript")))
        lines = []
        browser.alert_human("click it", log=lines.append)
        assert lines and "click it" in lines[0]


@pytest.mark.django_db
class TestChallengePatience:
    """A challenge that merely ran long must not kill a 930-card run. The job
    pauses, restarts the browser, and re-queues the card it was on."""

    @pytest.fixture
    def no_backup(self, monkeypatch):
        monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)

    @pytest.fixture
    def instant(self, monkeypatch):
        """No real waiting, but record how long each pause would have been."""
        pauses = []
        monkeypatch.setattr(runner.time, "sleep", lambda s: pauses.append(s))
        monkeypatch.setattr(runner, "random_delay", lambda *a, **kw: None)
        return pauses

    def _price_job(self, game, superuser):
        return services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )

    def test_a_slow_challenge_pauses_and_the_card_still_gets_done(
        self, game, owned_printing, superuser, monkeypatch, no_backup, instant
    ):
        from decimal import Decimal
        job = self._price_job(game, superuser)
        monkeypatch.setattr(runner, "build_driver", lambda **kw: StubDriver())

        calls = []

        def slow_then_fine(driver, url, log):
            calls.append(url)
            if len(calls) == 1:
                raise ScrapeBlocked("challenge did not clear", recoverable=True)
            return {"price_trend": Decimal("2.50"), "currency": "EUR"}

        monkeypatch.setattr(runner, "scrape_card_prices", slow_then_fine)
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.COMPLETED
        assert job.failed_items == 0
        owned_printing.refresh_from_db()
        assert owned_printing.current_price_trend == Decimal("2.50")
        assert instant, "should have paused before retrying"

    def test_an_outright_refusal_still_stops_immediately(
        self, game, owned_printing, superuser, monkeypatch, no_backup, instant
    ):
        job = self._price_job(game, superuser)
        monkeypatch.setattr(runner, "build_driver", lambda **kw: StubDriver())
        monkeypatch.setattr(runner, "scrape_card_prices", lambda *a, **kw: (_ for _ in ()).throw(
            ScrapeBlocked("Sorry, you have been blocked", recoverable=False)))
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.FAILED
        assert not instant, "a refusal must not sit there backing off"

    def test_patience_is_finite(
        self, game, owned_printing, superuser, monkeypatch, no_backup, instant
    ):
        from scraping.models import ScraperSettings
        config = ScraperSettings.load()
        config.challenge_attempts = 2
        config.save()

        job = self._price_job(game, superuser)
        monkeypatch.setattr(runner, "build_driver", lambda **kw: StubDriver())
        attempts = []

        def always_slow(driver, url, log):
            attempts.append(url)
            raise ScrapeBlocked("challenge did not clear", recoverable=True)

        monkeypatch.setattr(runner, "scrape_card_prices", always_slow)
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.FAILED
        assert len(attempts) == 3  # initial try + 2 patient retries

    def test_each_pause_is_longer_than_the_last(self):
        assert list(runner.CHALLENGE_BACKOFF) == sorted(runner.CHALLENGE_BACKOFF)
        assert runner.CHALLENGE_BACKOFF[0] >= 60


@pytest.mark.django_db
class TestPacingIsApplied:
    """The pace setting was wired into card discovery only — a price refresh,
    the job that hits Cardmarket hardest, ignored it entirely."""

    @pytest.fixture
    def no_backup(self, monkeypatch):
        monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)

    @pytest.fixture
    def recorded_pauses(self, monkeypatch):
        pauses = []
        monkeypatch.setattr(runner, "random_delay",
                            lambda lo=0, hi=0: pauses.append((lo, hi)))
        return pauses

    @pytest.fixture
    def second_printing(self, expansion, superuser):
        from catalog.normalize import normalize_name, slug_display_name
        from collection.services import set_quantity
        card = Printing.objects.create(
            expansion=expansion, slug="Agave-Dragon",
            display_name=slug_display_name("Agave-Dragon"),
            name_normalized=normalize_name("Agave-Dragon"),
        )
        set_quantity(card, 1, user=superuser)
        return card

    @pytest.fixture
    def slow_pace(self):
        from scraping.models import ScraperSettings
        config = ScraperSettings.load()
        config.min_item_delay, config.max_item_delay = 10, 20
        config.save()
        return config

    def test_price_refresh_paces_between_cards(
        self, game, owned_printing, second_printing, superuser,
        monkeypatch, no_backup, recorded_pauses, slow_pace
    ):
        from decimal import Decimal
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: StubDriver())
        monkeypatch.setattr(runner, "scrape_card_prices",
                            lambda driver, url, log: {"price_trend": Decimal("1"),
                                                      "currency": "EUR"})
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.COMPLETED
        # Two cards, one gap between them, using the configured range.
        assert (10, 20) in recorded_pauses

    def test_the_first_card_is_not_delayed(
        self, game, owned_printing, superuser, monkeypatch, no_backup,
        recorded_pauses, slow_pace
    ):
        from decimal import Decimal
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: StubDriver())
        monkeypatch.setattr(runner, "scrape_card_prices",
                            lambda driver, url, log: {"price_trend": Decimal("1"),
                                                      "currency": "EUR"})
        runner.run_job(job.pk)
        assert (10, 20) not in recorded_pauses  # only one card: no gap to pace

    def test_card_discovery_paces_between_sets(
        self, game, expansion, superuser, monkeypatch, no_backup,
        recorded_pauses, slow_pace
    ):
        Expansion.objects.create(game=game, slug="Second-Set",
                                 display_name="Second Set", fully_scraped=False)
        Expansion.objects.filter(pk=expansion.pk).update(fully_scraped=False)
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.DISCOVER_CARDS,
            params={"expansion": ""}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: StubDriver())
        monkeypatch.setattr(runner, "discover_cards",
                            lambda driver, seg, slug, log, deep_search: ({}, True))
        runner.run_job(job.pk)
        assert (10, 20) in recorded_pauses
