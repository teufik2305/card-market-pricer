"""Scrape job seeding, runner behavior (with fake scrapers/driver), and the
panel views. No Selenium runs here — the scraper functions are monkeypatched."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from catalog.models import Expansion, Printing
from catalog.normalize import normalize_name, slug_display_name
from collection.services import set_quantity
from pricing.models import PriceSnapshot
from scraping import runner, services
from scraping.models import ScrapeJob, ScrapeJobItem


class FakeDriver:
    def quit(self):
        pass


@pytest.fixture(autouse=True)
def _no_real_browser_work(monkeypatch):
    """The runner warms the browser up and paces itself between items now.
    Neither belongs in a test that never opens a browser."""
    monkeypatch.setattr(runner, "random_delay", lambda *a, **kw: None)


@pytest.fixture
def no_backup(monkeypatch):
    """Runner tests run against :memory: — skip the real backup step."""
    monkeypatch.setattr(runner, "_pre_job_backup", lambda job, log: None)


@pytest.fixture
def owned_printing(printing, superuser):
    set_quantity(printing, 2, user=superuser)
    return printing


@pytest.fixture
def second_printing(expansion, superuser):
    slug = "Agave-Dragon"
    card = Printing.objects.create(
        expansion=expansion, slug=slug,
        display_name=slug_display_name(slug), name_normalized=normalize_name(slug),
    )
    set_quantity(card, 1, user=superuser)
    return card


@pytest.mark.django_db
class TestJobSeeding:
    def test_refresh_prices_all_targets_only_owned(self, game, expansion, owned_printing, superuser):
        Printing.objects.create(  # unowned — must not be seeded
            expansion=expansion, slug="Some-Bulk", display_name="Some Bulk",
            name_normalized="somebulk",
        )
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        assert job.total_items == 1
        assert job.items.get().card == owned_printing

    def test_refresh_prices_stale_scope(self, game, owned_printing, second_printing, superuser):
        Printing.objects.filter(pk=owned_printing.pk).update(
            prices_updated_at=timezone.now()
        )
        Printing.objects.filter(pk=second_printing.pk).update(
            prices_updated_at=timezone.now() - timedelta(days=60)
        )
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "stale", "stale_days": 30}, user=superuser,
        )
        assert [i.card_id for i in job.items.all()] == [second_printing.pk]

    def test_unknown_expansion_rejected(self, game, owned_printing, superuser):
        with pytest.raises(services.JobError, match="Unknown expansion"):
            services.create_job(
                game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
                params={"scope": "expansion", "expansion": "Nope"}, user=superuser,
            )

    def test_empty_scope_rejected(self, game, expansion, superuser):
        with pytest.raises(services.JobError, match="Nothing to do"):
            services.create_job(
                game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
                params={"scope": "all"}, user=superuser,
            )

    def test_single_active_job_enforced(self, game, owned_printing, superuser):
        services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        with pytest.raises(services.JobError, match="already active"):
            services.create_job(
                game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
                params={"scope": "all"}, user=superuser,
            )

    def test_discover_cards_defaults_to_unfinished(self, game, expansion, superuser):
        Expansion.objects.create(
            game=game, slug="Done-Set", display_name="Done Set", fully_scraped=True
        )
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.DISCOVER_CARDS, params={}, user=superuser,
        )
        assert [i.expansion.slug for i in job.items.all()] == [expansion.slug]


@pytest.mark.django_db
class TestRunner:
    def test_discover_expansions(self, game, expansion, superuser, monkeypatch, no_backup):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.DISCOVER_EXPANSIONS,
            params={"last_n_years": 2}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())
        monkeypatch.setattr(
            runner, "discover_expansions",
            lambda driver, segment, years, log: ["Brand-New-Set", expansion.slug],
        )
        runner.run_job(job.pk)
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.COMPLETED
        assert job.total_items == 2 and job.processed_items == 2
        new = Expansion.objects.get(slug="Brand-New-Set")
        assert new.display_name == "Brand New Set"
        expansion.refresh_from_db()
        assert expansion.last_discovered_at is not None

    def test_discover_cards_is_additive(self, game, expansion, owned_printing, superuser, monkeypatch, no_backup):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.DISCOVER_CARDS,
            params={"expansion": expansion.slug}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())
        monkeypatch.setattr(
            runner, "discover_cards",
            lambda driver, seg, slug, log, deep_search: (
                {"Fresh-Card": Decimal("0.50"), owned_printing.slug: Decimal("9.99")}, True
            ),
        )
        runner.run_job(job.pk)
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.COMPLETED
        fresh = Printing.objects.get(slug="Fresh-Card")
        assert fresh.current_price_from == Decimal("0.50")
        assert fresh.name_normalized == "freshcard"
        owned_printing.refresh_from_db()
        assert owned_printing.current_price_from != Decimal("9.99")  # existing untouched
        expansion.refresh_from_db()
        assert expansion.fully_scraped is True

    def test_refresh_prices_snapshots_and_denormalizes(self, game, owned_printing, superuser, monkeypatch, no_backup):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())
        monkeypatch.setattr(
            runner, "scrape_card_prices",
            lambda driver, url, log: {
                "price_from": Decimal("3.10"), "price_trend": Decimal("5.55"),
                "price_30d_avg": Decimal("5.40"), "currency": "EUR",
            },
        )
        runner.run_job(job.pk)
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.COMPLETED
        snapshot = PriceSnapshot.objects.get(scrape_job=job)
        assert snapshot.price_trend == Decimal("5.55")
        assert snapshot.source == PriceSnapshot.Source.CARD_PAGE
        owned_printing.refresh_from_db()
        assert owned_printing.current_price_trend == Decimal("5.55")
        assert owned_printing.prices_updated_at is not None
        assert job.items.get().status == ScrapeJobItem.Status.DONE

    def test_refresh_prices_failure_marks_item_and_continues(
        self, game, owned_printing, second_printing, superuser, monkeypatch, no_backup
    ):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )

        def flaky(driver, url, log):
            if second_printing.slug in url:
                raise ValueError("boom")
            return {"price_trend": Decimal("1.00"), "currency": "EUR"}

        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())
        monkeypatch.setattr(runner, "scrape_card_prices", flaky)
        runner.run_job(job.pk)
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.COMPLETED_WITH_ERRORS
        assert job.failed_items == 1 and job.processed_items == 2
        failed = job.items.get(card=second_printing)
        assert failed.status == ScrapeJobItem.Status.FAILED
        assert "boom" in failed.message

    def test_cancellation_between_items(
        self, game, owned_printing, second_printing, superuser, monkeypatch, no_backup
    ):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )

        def cancel_after_first(driver, url, log):
            ScrapeJob.objects.filter(pk=job.pk).update(
                status=ScrapeJob.Status.CANCEL_REQUESTED
            )
            return {"price_trend": Decimal("1.00"), "currency": "EUR"}

        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())
        monkeypatch.setattr(runner, "scrape_card_prices", cancel_after_first)
        runner.run_job(job.pk)
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.CANCELLED
        assert job.processed_items == 1  # first item's work is saved
        assert PriceSnapshot.objects.filter(scrape_job=job).count() == 1


@pytest.mark.django_db
class TestPanelViews:
    def test_panel_requires_staff(self, client, game, superuser):
        from django.contrib.auth.models import User

        regular = User.objects.create_user("pleb", password="pw")
        client.force_login(regular)
        assert client.get("/scrape/").status_code == 302  # redirected to login

    def test_panel_renders_for_staff(self, client, game, superuser):
        client.force_login(superuser)
        response = client.get("/scrape/")
        assert response.status_code == 200
        assert "Public APIs" in response.text and "Cardmarket scraper" in response.text

    def test_create_job_launches_after_commit_and_redirects(
        self, client, game, owned_printing, superuser, monkeypatch,
        django_capture_on_commit_callbacks,
    ):
        launched = []
        monkeypatch.setattr(services, "launch_job", lambda job: launched.append(job.pk))
        client.force_login(superuser)
        with django_capture_on_commit_callbacks(execute=True):
            response = client.post("/scrape/jobs/", {
                "game": game.code, "job_type": "refresh_prices", "scope": "all",
            })
        job = ScrapeJob.objects.get()
        assert response.status_code == 302
        assert response.headers["Location"] == f"/scrape/jobs/{job.pk}/"
        assert launched == [job.pk]  # spawned via transaction.on_commit
        assert job.total_items == 1

    def test_create_job_error_is_visible_and_leaves_no_orphan(self, client, game, superuser):
        client.force_login(superuser)
        response = client.post("/scrape/jobs/", {
            "game": game.code, "job_type": "refresh_prices", "scope": "all",
        })
        # 200 on purpose: under hx-boost a 4xx body would never be swapped in.
        assert response.status_code == 200
        assert "Nothing to do" in response.text
        # the half-created job must have rolled back, not wedge the launcher
        assert ScrapeJob.objects.count() == 0

    def test_cancel_endpoint(self, client, game, owned_printing, superuser):
        client.force_login(superuser)
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        ScrapeJob.objects.filter(pk=job.pk).update(
            status=ScrapeJob.Status.RUNNING, heartbeat_at=timezone.now(),
        )
        response = client.post(f"/scrape/jobs/{job.pk}/cancel/")
        assert response.status_code == 200
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.CANCEL_REQUESTED


@pytest.mark.django_db
class TestStalledRecovery:
    """A dead runner process must never wedge the launcher forever."""

    def _running_job(self, game, superuser, **overrides):
        job = ScrapeJob.objects.create(
            game=game, job_type=ScrapeJob.Type.DISCOVER_EXPANSIONS,
            created_by=superuser,
        )
        defaults = {"status": ScrapeJob.Status.RUNNING, "heartbeat_at": timezone.now()}
        defaults.update(overrides)
        ScrapeJob.objects.filter(pk=job.pk).update(**defaults)
        job.refresh_from_db()
        return job

    def test_fresh_heartbeat_is_not_stalled(self, game, superuser):
        job = self._running_job(game, superuser)
        assert services.is_stalled(job) is False

    def test_stale_heartbeat_is_stalled(self, game, superuser):
        job = self._running_job(
            game, superuser, heartbeat_at=timezone.now() - timedelta(minutes=10)
        )
        assert services.is_stalled(job) is True

    def test_dead_pid_is_stalled(self, game, superuser):
        job = self._running_job(game, superuser, pid=2**22 + 1)  # not a real process
        assert services.is_stalled(job) is True

    def test_never_claimed_job_goes_stalled_after_grace(self, game, superuser):
        job = ScrapeJob.objects.create(
            game=game, job_type=ScrapeJob.Type.DISCOVER_EXPANSIONS, created_by=superuser,
        )
        assert services.is_stalled(job) is False  # inside the grace period
        ScrapeJob.objects.filter(pk=job.pk).update(
            created_at=timezone.now() - timedelta(minutes=10)
        )
        job.refresh_from_db()
        assert services.is_stalled(job) is True

    def test_cancel_on_stalled_job_force_cancels(self, game, superuser):
        job = self._running_job(
            game, superuser, heartbeat_at=timezone.now() - timedelta(minutes=10)
        )
        services.request_cancel(job)
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.CANCELLED  # not CANCEL_REQUESTED
        assert services.active_job() is None  # launcher freed

    def test_force_fail_endpoint(self, client, game, superuser):
        client.force_login(superuser)
        job = self._running_job(
            game, superuser, heartbeat_at=timezone.now() - timedelta(minutes=10)
        )
        response = client.post(f"/scrape/jobs/{job.pk}/force-fail/")
        assert response.status_code == 200
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.FAILED
        assert services.active_job() is None

    def test_force_fail_refuses_healthy_job(self, client, game, superuser):
        client.force_login(superuser)
        job = self._running_job(game, superuser)
        client.post(f"/scrape/jobs/{job.pk}/force-fail/")
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.RUNNING

    def test_progress_fragment_stops_polling_when_terminal(self, client, game, superuser):
        client.force_login(superuser)
        running = self._running_job(game, superuser)
        assert client.get(f"/scrape/jobs/{running.pk}/progress/").status_code == 200
        ScrapeJob.objects.filter(pk=running.pk).update(status=ScrapeJob.Status.COMPLETED)
        assert client.get(f"/scrape/jobs/{running.pk}/progress/").status_code == 286


@pytest.mark.django_db
class TestPriceStorageHonesty:
    def test_partial_prices_do_not_bump_staleness_timestamp(
        self, game, owned_printing, superuser, monkeypatch, no_backup
    ):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())
        monkeypatch.setattr(
            runner, "scrape_card_prices",
            lambda driver, url, log: {"price_trend": Decimal("5.55"), "currency": "EUR"},
        )
        before = owned_printing.prices_updated_at
        runner.run_job(job.pk)
        owned_printing.refresh_from_db()
        assert owned_printing.current_price_trend == Decimal("5.55")
        assert owned_printing.prices_updated_at == before  # still counts as stale
        item = job.items.get()
        assert item.status == ScrapeJobItem.Status.DONE
        assert "partial" in item.message

    def test_non_eur_prices_never_touch_denormalized_fields(
        self, game, owned_printing, superuser, monkeypatch, no_backup
    ):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())
        monkeypatch.setattr(
            runner, "scrape_card_prices",
            lambda driver, url, log: {
                "price_from": Decimal("1.00"), "price_trend": Decimal("2.00"),
                "price_30d_avg": Decimal("3.00"), "currency": "GBP",
            },
        )
        owned_printing.refresh_from_db()
        old_trend = owned_printing.current_price_trend
        runner.run_job(job.pk)
        snapshot = PriceSnapshot.objects.get(scrape_job=job)
        assert snapshot.currency == "GBP"
        assert snapshot.price_trend == Decimal("2.00")  # history keeps the truth
        owned_printing.refresh_from_db()
        assert owned_printing.current_price_trend == old_trend  # totals stay EUR

    def test_backup_failure_fails_the_job(self, game, owned_printing, superuser, monkeypatch):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )

        def boom(job, log):
            raise RuntimeError("disk full")

        monkeypatch.setattr(runner, "_pre_job_backup", boom)
        runner.run_job(job.pk)
        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.FAILED
        assert "disk full" in job.error
        assert services.active_job() is None


@pytest.mark.django_db
class TestJobsDoNotResume:
    """There is no resume. A terminal job is finished for good, and the record
    must say so instead of leaving items that look outstanding."""

    def test_cancelling_marks_unreached_items_skipped(
        self, game, owned_printing, second_printing, superuser, monkeypatch, no_backup
    ):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        assert job.total_items == 2
        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())

        def cancel_after_first(driver, url, log):
            ScrapeJob.objects.filter(pk=job.pk).update(
                status=ScrapeJob.Status.CANCEL_REQUESTED
            )
            return {"price_trend": Decimal("1.00"), "currency": "EUR"}

        monkeypatch.setattr(runner, "scrape_card_prices", cancel_after_first)
        runner.run_job(job.pk)

        job.refresh_from_db()
        assert job.status == ScrapeJob.Status.CANCELLED
        assert job.items.filter(status=ScrapeJobItem.Status.PENDING).count() == 0
        skipped = job.items.filter(status=ScrapeJobItem.Status.SKIPPED)
        assert skipped.count() == 1
        assert "cancelled" in skipped.first().message

    def test_a_cancelled_job_offers_no_way_to_restart_itself(
        self, client, game, owned_printing, superuser
    ):
        """Assert on controls, not prose: the page SHOULD say jobs don't resume,
        it just must not offer a button that pretends otherwise."""
        import re

        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        services.request_cancel(job)
        job.refresh_from_db()
        client.force_login(superuser)
        body = client.get(f"/scrape/jobs/{job.pk}/").text

        # Nothing posts back to this job any more — no cancel, no resume, nothing.
        assert f"/scrape/jobs/{job.pk}/" not in re.sub(
            rf'href="/scrape/jobs/{job.pk}/"', "", body
        ).replace(f"#{job.pk}", "")
        # And it says so out loud.
        assert "cannot be resumed" in body or "don" in body and "resume" in body

    def test_a_cancelled_job_states_what_was_never_attempted(
        self, client, game, owned_printing, second_printing, superuser
    ):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        services.request_cancel(job)
        client.force_login(superuser)
        body = client.get(f"/scrape/jobs/{job.pk}/").text
        assert "2 items were never attempted" in body

    def test_re_running_covers_only_what_is_still_outstanding(
        self, game, owned_printing, second_printing, superuser
    ):
        """The honest replacement for resume: scope excludes finished work."""
        from django.utils import timezone
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "stale", "stale_days": 30}, user=superuser,
        )
        assert job.total_items == 2
        services.request_cancel(job)
        # One card got priced before the cancel.
        Printing.objects.filter(pk=owned_printing.pk).update(
            prices_updated_at=timezone.now()
        )
        again = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "stale", "stale_days": 30}, user=superuser,
        )
        assert again.total_items == 1
        assert again.items.get().card_id == second_printing.pk


@pytest.mark.django_db
class TestRetryFailedItems:
    """Per-item results are only worth recording if you can act on them."""

    @pytest.fixture
    def job_with_failures(self, game, owned_printing, second_printing, superuser,
                          monkeypatch, no_backup):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())

        def flaky(driver, url, log):
            if owned_printing.slug in url:
                return {"price_trend": Decimal("1.00"), "currency": "EUR"}
            raise ValueError("page did not load")

        monkeypatch.setattr(runner, "scrape_card_prices", flaky)
        runner.run_job(job.pk)
        job.refresh_from_db()
        assert job.failed_items == 1
        return job

    def test_retry_queues_only_what_failed(self, job_with_failures, superuser):
        retry = services.create_retry_job(job_with_failures, user=superuser)
        assert retry.total_items == 1
        failed_card = job_with_failures.items.get(status=ScrapeJobItem.Status.FAILED).card_id
        assert retry.items.get().card_id == failed_card

    def test_retry_records_what_it_came_from(self, job_with_failures, superuser):
        retry = services.create_retry_job(job_with_failures, user=superuser)
        assert retry.params["retry_of"] == job_with_failures.pk
        assert retry.job_type == job_with_failures.job_type

    def test_nothing_to_retry_is_refused(self, game, owned_printing, superuser,
                                         monkeypatch, no_backup):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.REFRESH_PRICES,
            params={"scope": "all"}, user=superuser,
        )
        monkeypatch.setattr(runner, "build_driver", lambda **kw: FakeDriver())
        monkeypatch.setattr(runner, "scrape_card_prices",
                            lambda driver, url, log: {"price_trend": Decimal("1"), "currency": "EUR"})
        runner.run_job(job.pk)
        with pytest.raises(services.JobError, match="no failed items"):
            services.create_retry_job(job, user=superuser)

    def test_single_unit_jobs_have_nothing_to_retry(self, game, superuser):
        job = services.create_job(
            game=game, job_type=ScrapeJob.Type.IMPORT_CATALOG, params={}, user=superuser,
        )
        with pytest.raises(services.JobError, match="no per-item results"):
            services.create_retry_job(job, user=superuser)

    def test_the_button_appears_only_on_a_finished_job_with_failures(
        self, client, job_with_failures, superuser
    ):
        client.force_login(superuser)
        body = client.get(f"/scrape/jobs/{job_with_failures.pk}/").text
        assert "Retry 1 failed item" in body
