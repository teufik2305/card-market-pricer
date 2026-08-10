import pytest

from collection.models import Holding, QuantityChange
from collection.services import set_quantity


@pytest.fixture
def logged_in(client, superuser):
    client.force_login(superuser)
    return client


@pytest.mark.django_db
class TestAuth:
    def test_anonymous_redirected(self, client, printing):
        assert client.get("/").status_code == 302

    def test_qty_endpoint_requires_login(self, client, printing):
        response = client.post(f"/holdings/qty/{printing.pk}/", {"op": "inc"})
        assert response.status_code == 302


@pytest.mark.django_db
class TestPages:
    def test_dashboard(self, logged_in, printing):
        assert logged_in.get("/").status_code == 200

    def test_expansion_list_and_detail(self, logged_in, printing):
        game = printing.expansion.game.code
        assert logged_in.get(f"/g/{game}/expansions/").status_code == 200
        assert logged_in.get(
            f"/g/{game}/expansions/{printing.expansion.slug}/"
        ).status_code == 200

    def test_card_browser_and_detail(self, logged_in, printing, superuser):
        set_quantity(printing, 2, user=superuser)
        game = printing.expansion.game.code
        assert logged_in.get(f"/g/{game}/cards/").status_code == 200
        response = logged_in.get(f"/g/{game}/cards/{printing.pk}/")
        assert response.status_code == 200
        assert "Chaos Dragon Levianeer" in response.text

    def test_unknown_game_404s(self, logged_in, printing):
        assert logged_in.get("/g/pokemon/expansions/").status_code == 404


@pytest.mark.django_db
class TestQuantityEndpoint:
    def test_inc_creates_holding_and_toast(self, logged_in, printing):
        response = logged_in.post(f"/holdings/qty/{printing.pk}/", {"op": "inc"})
        assert response.status_code == 200
        assert Holding.objects.get(card=printing).quantity == 1
        assert "toast success" in response.text
        assert response.headers.get("HX-Trigger") == "qty-changed"

    def test_set_value(self, logged_in, printing):
        logged_in.post(f"/holdings/qty/{printing.pk}/", {"op": "set", "value": "7"})
        assert Holding.objects.get(card=printing).quantity == 7

    def test_invalid_value_returns_error_toast_and_changes_nothing(self, logged_in, printing):
        response = logged_in.post(f"/holdings/qty/{printing.pk}/", {"op": "set", "value": "-2"})
        assert response.status_code == 200
        assert "toast error" in response.text
        assert not Holding.objects.filter(card=printing).exists()

    def test_undo_flow(self, logged_in, printing, superuser):
        set_quantity(printing, 4, user=superuser)
        _, change = set_quantity(printing, 1, user=superuser)
        response = logged_in.post(f"/holdings/changes/{change.pk}/undo/")
        assert response.status_code == 200
        assert Holding.objects.get(card=printing).quantity == 4
        assert QuantityChange.objects.count() == 3

    def test_get_not_allowed(self, logged_in, printing):
        assert logged_in.get(f"/holdings/qty/{printing.pk}/").status_code == 405
