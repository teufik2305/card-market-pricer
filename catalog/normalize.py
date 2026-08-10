"""Name normalization — the single join key between Cardmarket slugs, catalog
card names, want-list input, and decklist entries.

Collapsing ALL non-alphanumerics (rather than mapping dashes to spaces) makes
real-hyphen names (Odd-Eyes, Mekk-Knight), colons, apostrophes and the observed
slug drift ("World-Legacys-Secret" vs "World-Legacy-s-Secret") normalize to the
same string with no special cases.
"""

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Cardmarket rarity suffixes seen on YuGiOh slugs, longest first so e.g.
# -Quarter-Century-Secret-Rare is stripped before -Secret-Rare.
# Bare-word tails (-Ghost, -Gold, ...) are traps: real card names end in them
# (Magical-Ghost, Elemental-HERO-Captain-Gold), so resolution must always try
# the UNSTRIPPED slug first (see decks resolution pipeline, later phase).
YGO_RARITY_SUFFIXES = [
    "-Duel-Terminal-Normal-Parallel-Rare",
    "-Duel-Terminal-Rare-Parallel-Rare",
    "-Quarter-Century-Secret-Rare",
    "-Prismatic-Ultimate-Rare",
    "-Prismatic-Secret-Rare",
    "-Platinum-Secret-Rare",
    "-Secret-Parallel-Rare",
    "-Normal-Parallel-Rare",
    "-Super-Parallel-Rare",
    "-Ultra-Parallel-Rare",
    "-Extra-Secret-Rare",
    "-Gold-Secret-Rare",
    "-Premium-Gold-Rare",
    "-Collectors-Rare",
    "-Shatterfoil-Rare",
    "-Starfoil-Rare",
    "-Starlight-Rare",
    "-Millennium-Rare",
    "-Ultimate-Rare",
    "-Parallel-Rare",
    "-Pharaohs-Rare",
    "-Mosaic-Rare",
    "-Secret-Rare",
    "-Ultra-Rare",
    "-Super-Rare",
    "-Ghost-Rare",
    "-Gold-Rare",
    "-Short-Print",
    "-Shatterfoil",
    "-Oversized",
    "-Special",
    "-Common",
    "-Skill",
    # Bare "-Rare" is last: it is a suffix of almost every entry above, so it
    # must only apply once the longer, more specific ones have failed.
    "-Rare",
]

# Cardmarket writes the art variant both ways: -V2 and -V-2.
_VERSION_SUFFIX = re.compile(r"-V-?\d+$")
# A bare trailing number is also an art variant ("Dark-Magician-3"), but it is
# genuinely ambiguous with names that end in a digit, so callers treat this as
# a last resort.
_NUMBER_SUFFIX = re.compile(r"-\d+$")


def normalize_name(raw: str) -> str:
    """Lowercase, strip diacritics, drop every non-alphanumeric character."""
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _NON_ALNUM.sub("", ascii_only.lower())


def strip_rarity_suffix(slug: str) -> str:
    for suffix in YGO_RARITY_SUFFIXES:
        if slug.endswith(suffix):
            return slug[: -len(suffix)]
    return slug


def strip_version_suffix(slug: str) -> str:
    return _VERSION_SUFFIX.sub("", slug)


def strip_number_suffix(slug: str) -> str:
    return _NUMBER_SUFFIX.sub("", slug)


def slug_display_name(slug: str) -> str:
    return slug.replace("-", " ")
