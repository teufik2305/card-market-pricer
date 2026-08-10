import pytest
from django.contrib.auth.models import User

from catalog.models import Expansion, Game, Printing
from catalog.normalize import normalize_name, slug_display_name


@pytest.fixture
def superuser(db):
    return User.objects.create_superuser("boss", "boss@example.com", "pw")


@pytest.fixture
def game(db):
    return Game.objects.create(code="yugioh", name="Yu-Gi-Oh!", cardmarket_segment="YuGiOh")


@pytest.fixture
def expansion(game):
    return Expansion.objects.create(
        game=game, slug="Soul-Fusion", display_name="Soul Fusion"
    )


@pytest.fixture
def printing(expansion):
    slug = "Chaos-Dragon-Levianeer"
    return Printing.objects.create(
        expansion=expansion,
        slug=slug,
        display_name=slug_display_name(slug),
        name_normalized=normalize_name(slug),
        current_price_from="2.99",
        current_price_trend="5.36",
        current_price_30d="5.71",
    )


@pytest.fixture
def digimon_game(db):
    from catalog.models import Game
    return Game.objects.create(
        code="digimon", name="Digimon", cardmarket_segment="Digimon"
    )


@pytest.fixture
def digimon_expansion(digimon_game):
    from catalog.models import Expansion
    return Expansion.objects.create(
        game=digimon_game, slug="Across-Time", display_name="Across Time"
    )
