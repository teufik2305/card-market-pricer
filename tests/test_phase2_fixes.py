"""Regression tests for the findings confirmed by the Phase 2 review."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import User

from catalog.models import Expansion, Game, Printing
from catalog.normalize import normalize_name, slug_display_name
from collection.models import Holding, SellPackage, SellPackageItem
from collection.services import (
    QuantityError,
    apply_bulk,
    apply_package_rule,
    finalize_sale,
    preview_bulk,
    preview_package_rule,
    set_quantity,
    undo_batch,
)


@pytest.fixture
def cards(expansion):
    made = []
    for slug in ["Card-A", "Card-B"]:
        made.append(Printing.objects.create(
            expansion=expansion, slug=slug, display_name=slug_display_name(slug),
            name_normalized=normalize_name(slug),
            current_price_from="1.00", current_price_trend="1.00", current_price_30d="1.00",
        ))
    return made


@pytest.fixture
def other_user(db):
    return User.objects.create_user("other", password="pw")


@pytest.fixture
def logged_in(client, superuser):
    client.force_login(superuser)
    return client


@pytest.mark.django_db
class TestKeeperSurvivesUndo:
    def test_undo_batch_does_not_undo_its_own_rows(self, cards, superuser):
        batch_id, _ = apply_bulk(cards, "set", 4, user=superuser)
        reverted, skipped = undo_batch(batch_id, user=superuser)
        assert (reverted, skipped) == (2, 0)
        assert set(Holding.objects.values_list("quantity", flat=True)) == {0}


@pytest.mark.django_db
class TestOwnerScoping:
    def test_bulk_uses_only_the_acting_users_holdings(self, cards, superuser, other_user):
        set_quantity(cards[0], 4, user=superuser)
        preview = preview_bulk(cards, "inc", 0, user=other_user)
        assert all(current == 0 for _card, current, _new in preview["changing"])
        apply_bulk(cards, "inc", 0, user=other_user)
        assert Holding.objects.get(card=cards[0], owner=superuser).quantity == 4
        assert Holding.objects.get(card=cards[0], owner=other_user).quantity == 1

    def test_finalize_sale_never_touches_another_owners_holding(
        self, game, cards, superuser, other_user
    ):
        set_quantity(cards[0], 10, user=superuser)
        package = SellPackage.objects.create(name="Theft", game=game, owner=other_user)
        SellPackageItem.objects.create(package=package, card=cards[0], quantity=5)
        with pytest.raises(QuantityError, match="only 0 sellable"):
            finalize_sale(package, user=other_user)
        assert Holding.objects.get(card=cards[0], owner=superuser).quantity == 10


@pytest.mark.django_db
class TestBulkValueHandling:
    def _data(self, game, expansion, **extra):
        data = {"game": game.code, "expansion": expansion.slug, "q": "", "owned": "0"}
        data.update(extra)
        return data

    def test_empty_value_is_an_error_not_zero(self, logged_in, game, expansion, cards, superuser):
        set_quantity(cards[0], 3, user=superuser)
        response = logged_in.post(
            "/holdings/bulk/apply/", self._data(game, expansion, op="set", value="")
        )
        assert "Enter a quantity first" in response.text
        assert Holding.objects.get(card=cards[0]).quantity == 3  # untouched

    def test_typed_value_reaches_the_service(self, logged_in, game, expansion, cards):
        logged_in.post(
            "/holdings/bulk/apply/", self._data(game, expansion, op="set", value="4")
        )
        assert set(Holding.objects.values_list("quantity", flat=True)) == {4}

    def test_bulk_value_input_has_a_name_attribute(self, logged_in, game, expansion, cards):
        """htmx drops nameless inputs from hx-include, so 'set all to N' would
        silently fall back to a default."""
        page = logged_in.get(f"/g/{game.code}/expansions/{expansion.slug}/").text
        assert 'id="bulk-value" name="value"' in page


@pytest.mark.django_db
class TestPackageIntegrity:
    def _package(self, game, superuser):
        return SellPackage.objects.create(name="Lot", game=game, owner=superuser)

    def test_cross_game_card_is_rejected(self, logged_in, game, cards, superuser):
        other_game = Game.objects.create(
            code="digimon", name="Digimon", cardmarket_segment="Digimon"
        )
        other_exp = Expansion.objects.create(
            game=other_game, slug="Across-Time", display_name="Across Time"
        )
        foreign = Printing.objects.create(
            expansion=other_exp, slug="Agumon", display_name="Agumon",
            name_normalized="agumon",
        )
        package = self._package(game, superuser)
        response = logged_in.post(f"/g/{game.code}/packages/{package.pk}/add/",
                                  {"card_id": foreign.pk, "quantity": "1"})
        assert response.status_code == 404
        assert package.items.count() == 0

    def test_package_from_another_game_url_is_404(self, logged_in, game, superuser):
        Game.objects.create(code="digimon", name="Digimon", cardmarket_segment="Digimon")
        package = self._package(game, superuser)
        assert logged_in.get(f"/g/digimon/packages/{package.pk}/").status_code == 404

    def test_empty_package_does_not_leak_to_other_games(self, logged_in, game, superuser):
        Game.objects.create(code="digimon", name="Digimon", cardmarket_segment="Digimon")
        self._package(game, superuser)  # empty, never had items added
        assert "Lot" in logged_in.get(f"/g/{game.code}/packages/").text
        assert "Lot" not in logged_in.get("/g/digimon/packages/").text

    def test_sold_package_cannot_be_mutated(self, logged_in, game, cards, superuser):
        set_quantity(cards[0], 2, user=superuser)
        package = self._package(game, superuser)
        SellPackageItem.objects.create(
            package=package, card=cards[0], quantity=1, unit_price_at_listing="1.00"
        )
        finalize_sale(package, user=superuser)

        added = logged_in.post(f"/g/{game.code}/packages/{package.pk}/add/",
                               {"card_id": cards[1].pk, "quantity": "1"})
        assert "frozen" in added.text
        removed = logged_in.post(
            f"/g/{game.code}/packages/{package.pk}/items/{package.items.first().pk}/remove/"
        )
        assert "frozen" in removed.text
        assert package.items.count() == 1  # unchanged

    def test_bad_add_quantity_rejected(self, logged_in, game, cards, superuser):
        set_quantity(cards[0], 1, user=superuser)
        package = self._package(game, superuser)
        response = logged_in.post(f"/g/{game.code}/packages/{package.pk}/add/",
                                  {"card_id": cards[0].pk, "quantity": "0"})
        assert "between 1 and 999" in response.text
        assert package.items.count() == 0

    def test_topping_up_keeps_the_original_listing_price(self, logged_in, game, cards, superuser):
        set_quantity(cards[0], 5, user=superuser)
        package = self._package(game, superuser)
        logged_in.post(f"/g/{game.code}/packages/{package.pk}/add/",
                       {"card_id": cards[0].pk, "quantity": "1"})
        Printing.objects.filter(pk=cards[0].pk).update(current_price_trend=Decimal("50.00"))
        logged_in.post(f"/g/{game.code}/packages/{package.pk}/add/",
                       {"card_id": cards[0].pk, "quantity": "2"})
        item = package.items.get()
        assert item.quantity == 3
        assert item.unit_price_at_listing == Decimal("1.00")

    def test_invalid_sold_price_is_a_clean_error(self, logged_in, game, cards, superuser):
        set_quantity(cards[0], 2, user=superuser)
        package = self._package(game, superuser)
        SellPackageItem.objects.create(
            package=package, card=cards[0], quantity=1, unit_price_at_listing="1.00"
        )
        response = logged_in.post(f"/g/{game.code}/packages/{package.pk}/finalize/",
                                  {"sold_price": "not-a-price"})
        assert response.status_code == 200
        assert "not a valid price" in response.text
        package.refresh_from_db()
        assert package.status == SellPackage.Status.DRAFT
        assert Holding.objects.get(card=cards[0]).quantity == 2  # nothing decremented

    def test_package_list_orders_drafts_before_cancelled(self, logged_in, game, superuser):
        SellPackage.objects.create(name="Zed draft", game=game,
                                   status=SellPackage.Status.DRAFT)
        SellPackage.objects.create(name="Abe cancelled", game=game,
                                   status=SellPackage.Status.CANCELLED)
        body = logged_in.get(f"/g/{game.code}/packages/").text
        assert body.index("Zed draft") < body.index("Abe cancelled")


@pytest.mark.django_db
class TestExportParamRobustness:
    @pytest.fixture
    def collection(self, cards, superuser):
        set_quantity(cards[0], 1, user=superuser)
        return cards

    @pytest.mark.parametrize("value", ["nan", "NaN", "inf", "Infinity", "-inf", "abc", ""])
    def test_non_finite_min_price_falls_back(self, logged_in, game, collection, value):
        response = logged_in.get(f"/g/{game.code}/export.csv?min_price={value}")
        assert response.status_code == 200
        body = b"".join(response.streaming_content).decode()
        assert len(body.strip().splitlines()) == 2  # header + the one owned card

    def test_non_finite_min_price_on_xlsx(self, logged_in, game, collection):
        assert logged_in.get(f"/g/{game.code}/export.xlsx?min_price=nan").status_code == 200

    def test_export_links_opt_out_of_boost(self, logged_in, game, collection):
        """hx-boost fetches a click via ajax and throws the body away, so an
        export link without the opt-out downloads nothing at all. Assert the
        property on every export link rather than a count — they live in the
        sidebar now and may appear on the page as well."""
        import re

        body = logged_in.get("/").text
        links = re.findall(r"<a\b[^>]*?href=\"[^\"]*/export\.(?:xlsx|csv)[^\"]*\"[^>]*>", body)
        assert links, "no export links rendered"
        for link in links:
            assert 'hx-boost="false"' in link and "download" in link, link


@pytest.mark.django_db
class TestAuditIntegrity:
    def test_a_sale_is_audited_against_reservations(self, game, cards, superuser):
        """The keep-list became a package; finalize_sale must still refuse to
        sell copies that are reserved."""
        set_quantity(cards[0], 3, user=superuser)
        keep = SellPackage.objects.create(
            name="Keep", game=game, owner=superuser, kind=SellPackage.Kind.KEEP
        )
        SellPackageItem.objects.create(package=keep, card=cards[0], quantity=2)
        lot = SellPackage.objects.create(name="Lot", game=game, owner=superuser)
        SellPackageItem.objects.create(package=lot, card=cards[0], quantity=2)
        with pytest.raises(QuantityError, match="only 1 sellable"):
            finalize_sale(lot, user=superuser)
        assert Holding.objects.get(card=cards[0]).quantity == 3  # untouched


@pytest.mark.django_db
class TestPackageRules:
    """Building a lot by rule: bulk under a price, duplicates, a set, an
    archetype. The arithmetic that matters is 'spare' — never offering a copy
    that is reserved or already committed here."""

    @pytest.fixture
    def stock(self, expansion, superuser):
        """Three cards: cheap ×4, mid ×2, expensive ×1."""
        made = {}
        for slug, price, qty in [("Cheap", "0.10", 4), ("Mid", "2.00", 2), ("Pricey", "25.00", 1)]:
            card = Printing.objects.create(
                expansion=expansion, slug=slug, display_name=slug,
                name_normalized=slug.lower(),
                current_price_from=price, current_price_trend=price, current_price_30d=price,
            )
            set_quantity(card, qty, user=superuser)
            made[slug] = card
        return made

    def _package(self, game, superuser, kind=SellPackage.Kind.SELL):
        return SellPackage.objects.create(name="Lot", game=game, owner=superuser, kind=kind)

    def test_bulk_rule_takes_only_cards_under_the_price(self, game, stock, superuser):
        package = self._package(game, superuser)
        preview = preview_package_rule(
            package, user=superuser, max_price=Decimal("0.40")
        )
        assert preview["cards"] == 1 and preview["copies"] == 4
        assert preview["candidates"][0]["card"] == stock["Cheap"]

    def test_duplicates_rule_keeps_one_of_each(self, game, stock, superuser):
        package = self._package(game, superuser)
        preview = preview_package_rule(package, user=superuser, keep_each=1)
        got = {c["card"].slug: c["quantity"] for c in preview["candidates"]}
        assert got == {"Cheap": 3, "Mid": 1}          # Pricey ×1 has no spare
        assert "Pricey" not in got

    def test_high_value_rule(self, game, stock, superuser):
        package = self._package(game, superuser)
        preview = preview_package_rule(package, user=superuser, min_price=Decimal("10"))
        assert [c["card"].slug for c in preview["candidates"]] == ["Pricey"]

    def test_reserved_copies_are_never_offered(self, game, stock, superuser):
        keep = self._package(game, superuser, kind=SellPackage.Kind.KEEP)
        SellPackageItem.objects.create(package=keep, card=stock["Cheap"], quantity=3)
        package = self._package(game, superuser)
        preview = preview_package_rule(package, user=superuser, max_price=Decimal("0.40"))
        assert preview["copies"] == 1  # 4 owned - 3 reserved

    def test_running_the_same_rule_twice_adds_nothing_more(self, game, stock, superuser):
        package = self._package(game, superuser)
        cards, copies = apply_package_rule(package, user=superuser, max_price=Decimal("0.40"))
        assert (cards, copies) == (1, 4)
        with pytest.raises(QuantityError, match="Nothing matches"):
            apply_package_rule(package, user=superuser, max_price=Decimal("0.40"))
        assert package.items.get().quantity == 4     # not doubled

    def test_prices_freeze_at_the_moment_of_adding(self, game, stock, superuser):
        package = self._package(game, superuser)
        apply_package_rule(package, user=superuser, max_price=Decimal("0.40"))
        Printing.objects.filter(pk=stock["Cheap"].pk).update(current_price_trend=Decimal("9.99"))
        assert package.items.get().unit_price_at_listing == Decimal("0.10")

    def test_a_price_rule_ignores_cards_with_no_price(self, game, expansion, superuser):
        unpriced = Printing.objects.create(
            expansion=expansion, slug="Unpriced", display_name="Unpriced",
            name_normalized="unpriced",
        )
        set_quantity(unpriced, 5, user=superuser)
        package = self._package(game, superuser)
        preview = preview_package_rule(package, user=superuser, max_price=Decimal("0.40"))
        assert preview["cards"] == 0

    def test_the_basis_is_respected(self, game, expansion, superuser):
        card = Printing.objects.create(
            expansion=expansion, slug="Split", display_name="Split",
            name_normalized="split",
            current_price_from="0.10", current_price_trend="5.00", current_price_30d="5.00",
        )
        set_quantity(card, 1, user=superuser)
        package = self._package(game, superuser)
        assert preview_package_rule(
            package, user=superuser, basis="from", max_price=Decimal("0.40")
        )["cards"] == 1
        assert preview_package_rule(
            package, user=superuser, basis="trend", max_price=Decimal("0.40")
        )["cards"] == 0

    def test_a_sold_package_rejects_rules(self, game, stock, superuser):
        package = self._package(game, superuser)
        SellPackageItem.objects.create(package=package, card=stock["Mid"], quantity=1,
                                       unit_price_at_listing="2.00")
        finalize_sale(package, user=superuser)
        with pytest.raises(QuantityError, match="frozen"):
            apply_package_rule(package, user=superuser, max_price=Decimal("0.40"))

    def test_a_keep_package_stores_no_listing_price(self, game, stock, superuser):
        keep = self._package(game, superuser, kind=SellPackage.Kind.KEEP)
        apply_package_rule(keep, user=superuser, max_price=Decimal("0.40"))
        assert keep.items.get().unit_price_at_listing is None

    def test_the_preview_matches_what_apply_does(self, game, stock, superuser):
        package = self._package(game, superuser)
        preview = preview_package_rule(package, user=superuser, keep_each=1)
        cards, copies = apply_package_rule(package, user=superuser, keep_each=1)
        assert (cards, copies) == (preview["cards"], preview["copies"])
