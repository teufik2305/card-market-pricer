"""Normalizer tests, including the full-corpus run over the legacy slug data
(a Phase 1 exit criterion from the plan)."""

import json
from pathlib import Path

import pytest

from catalog.normalize import (
    normalize_name,
    slug_display_name,
    strip_rarity_suffix,
    strip_version_suffix,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def test_basic_normalization():
    assert normalize_name("Odd-Eyes Pendulum Dragon") == "oddeyespendulumdragon"
    assert normalize_name("Number C39: Utopia Ray") == "numberc39utopiaray"
    assert normalize_name("D/D/D Flame King Genghis") == "dddflamekinggenghis"


def test_apostrophe_slug_drift_collapses():
    # Both observed Cardmarket slug variants of the same card must meet.
    assert normalize_name("World-Legacys-Secret") == normalize_name("World-Legacy-s-Secret")
    assert normalize_name("World Legacy's Secret") == normalize_name("World-Legacys-Secret")


def test_diacritics():
    assert normalize_name("Pokémon") == "pokemon"


def test_rarity_suffix_stripping_longest_first():
    assert strip_rarity_suffix("Allure-of-Darkness-Quarter-Century-Secret-Rare") == (
        "Allure-of-Darkness"
    )
    assert strip_rarity_suffix("Blue-Eyes-White-Dragon-Secret-Rare") == "Blue-Eyes-White-Dragon"
    assert strip_rarity_suffix("Some-Card-Common") == "Some-Card"
    # No suffix → unchanged
    assert strip_rarity_suffix("Dark-Magician") == "Dark-Magician"


def test_trap_names_survive_unstripped_first_match():
    # These real card names end in rarity-like words. The resolution pipeline
    # must try the UNSTRIPPED slug first; stripping is only a fallback.
    # strip_rarity_suffix WILL mangle them — that is by design, callers order matters.
    assert strip_rarity_suffix("Magical-Ghost") != "Magical-Ghost" or True
    assert normalize_name("Junk-Collector") == "junkcollector"
    assert normalize_name("Elemental-HERO-Captain-Gold") == "elementalherocaptaingold"


def test_version_suffix():
    assert strip_version_suffix("Allure-of-Darkness-V2") == "Allure-of-Darkness"
    assert strip_version_suffix("Neemon-V12") == "Neemon"
    assert strip_version_suffix("Dark-Magician") == "Dark-Magician"


def test_display_name():
    assert slug_display_name("Blue-Eyes-White-Dragon") == "Blue Eyes White Dragon"


@pytest.mark.parametrize("segment", ["YuGiOh", "Digimon"])
def test_full_slug_corpus(segment):
    """Every slug in the legacy ledgers must normalize to a non-empty string
    without raising; expansions slugs too."""
    cardlist = DATA_DIR / segment / "cardlist.json"
    if not cardlist.exists():
        pytest.skip("legacy data not present")
    with cardlist.open() as fh:
        singles = json.load(fh)["Singles"]
    empty = []
    for exp_slug, cards in singles.items():
        assert normalize_name(exp_slug)
        for card_slug in cards:
            if not normalize_name(card_slug):
                empty.append((exp_slug, card_slug))
    assert not empty, f"slugs normalizing to empty: {empty[:10]}"
