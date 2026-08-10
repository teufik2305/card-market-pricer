"""Regression tests for the findings confirmed by the adversarial review."""

import pytest
from django.contrib.auth.models import User
from django.db.models import ProtectedError
from django.utils import timezone

from collection.models import Holding, QuantityChange
from collection.services import QuantityError, set_quantity, undo_change
from pricing.models import PriceSnapshot
from scraping.models import ScrapeJob


@pytest.mark.django_db
class TestStaleUndoGuard:
    def test_undoing_superseded_change_is_refused(self, printing, superuser):
        _, c1 = set_quantity(printing, 5, user=superuser)
        set_quantity(printing, 8, user=superuser)  # newer edit supersedes c1
        with pytest.raises(QuantityError, match="superseded"):
            undo_change(c1, user=superuser)
        assert Holding.objects.get(card=printing).quantity == 8  # untouched

    def test_undo_endpoint_returns_error_toast_on_stale(self, client, printing, superuser):
        client.force_login(superuser)
        _, c1 = set_quantity(printing, 5, user=superuser)
        set_quantity(printing, 8, user=superuser)
        response = client.post(f"/holdings/changes/{c1.pk}/undo/")
        assert response.status_code == 200
        assert "toast error" in response.text
        assert Holding.objects.get(card=printing).quantity == 8


@pytest.mark.django_db
class TestOwnerScoping:
    def test_two_users_get_separate_holdings(self, printing, superuser):
        other = User.objects.create_user("other", password="pw")
        set_quantity(printing, 3, user=superuser)
        set_quantity(printing, 1, user=other)
        assert Holding.objects.filter(card=printing).count() == 2
        assert Holding.objects.get(card=printing, owner=superuser).quantity == 3
        assert Holding.objects.get(card=printing, owner=other).quantity == 1

    def test_undo_mutates_the_changes_own_holding(self, printing, superuser):
        other = User.objects.create_user("other", password="pw")
        _, change = set_quantity(printing, 3, user=superuser)
        set_quantity(printing, 9, user=other)
        holding, _ = undo_change(change, user=superuser)
        assert holding.owner == superuser
        assert Holding.objects.get(card=printing, owner=other).quantity == 9


@pytest.mark.django_db
class TestAppendOnlyQuerysetGuard:
    def test_queryset_update_blocked(self, printing, superuser):
        set_quantity(printing, 1, user=superuser)
        with pytest.raises(TypeError, match="append-only"):
            QuantityChange.objects.all().update(new_quantity=99)

    def test_queryset_delete_blocked(self, printing, superuser):
        set_quantity(printing, 1, user=superuser)
        with pytest.raises(TypeError, match="append-only"):
            QuantityChange.objects.all().delete()

    def test_deleting_scrape_job_with_snapshots_is_protected(self, printing, game, superuser):
        job = ScrapeJob.objects.create(game=game, job_type=ScrapeJob.Type.REFRESH_PRICES)
        PriceSnapshot.objects.create(
            card=printing, price_trend="1.00", observed_at=timezone.now(),
            source=PriceSnapshot.Source.CARD_PAGE, scrape_job=job,
        )
        with pytest.raises(ProtectedError):
            job.delete()


@pytest.mark.django_db
class TestListViewHtmx:
    """Boosted navigation must get full pages; only targeted filter swaps get partials."""

    @pytest.fixture
    def logged_in(self, client, superuser):
        client.force_login(superuser)
        return client

    def test_boosted_request_gets_full_page(self, logged_in, printing):
        game = printing.expansion.game.code
        response = logged_in.get(
            f"/g/{game}/cards/", HTTP_HX_REQUEST="true", HTTP_HX_BOOSTED="true"
        )
        assert "<nav" in response.text  # full base template, not bare rows

    def test_targeted_filter_swap_gets_panel_partial(self, logged_in, printing):
        """A targeted swap returns just the results panel — no app shell."""
        game = printing.expansion.game.code
        response = logged_in.get(
            f"/g/{game}/cards/?owned=0",
            HTTP_HX_REQUEST="true", HTTP_HX_TARGET="browser-panel",
        )
        text = response.text
        assert printing.display_name in text
        assert "app-shell" not in text and "<aside" not in text

    def test_owned_toggle_off_shows_unowned(self, logged_in, printing):
        game = printing.expansion.game.code
        # printing has qty 0; owned=0 (from the hidden input) must include it
        response = logged_in.get(f"/g/{game}/cards/?owned=0")
        assert "Chaos Dragon Levianeer" in response.text
        response = logged_in.get(f"/g/{game}/cards/?owned=1")
        assert "Chaos Dragon Levianeer" not in response.text
