"""Link Cardmarket Printings to canonical CardPieces.

This is the join that makes everything else possible: card art, metadata
filters, and eventually deck matching all live on the piece, while quantities
and prices live on the printing.

Two very different problems:

* **Digimon — parse, don't match.** The set number IS the identity and it is
  embedded in the slug (``Achillesmon-BT10-040`` → ``BT10-040``), in exactly the
  format digimoncard.io uses. Name matching would be hopeless here anyway: 3,336
  of 4,387 Digimon cards share a name with another card.
* **YuGiOh — ordered exact attempts, then cautious fuzzy.** Try the untouched
  slug first, then strip a rarity suffix, then a ``-V2`` art suffix. Order
  matters: real cards end in rarity words (``Junk-Collector``,
  ``Elemental-HERO-Captain-Gold``), so stripping first would mangle them.

Fuzzy never auto-accepts below the high cutoff, and never resolves to a piece
whose normalized name is shared by another card. Anything left over is a review
queue entry, not a guess.
"""

import logging
import re
from collections import defaultdict

from django.db import transaction

from .models import CardPiece, Printing
from .normalize import (
    normalize_name,
    strip_number_suffix,
    strip_rarity_suffix,
    strip_version_suffix,
)

logger = logging.getLogger(__name__)

AUTO_FUZZY_CUTOFF = 96
REVIEW_CUTOFF = 88

# Digimon card numbers. Not anchored to the end: Cardmarket appends rarity and
# alt-art suffixes (-U, -AA, -V1, -P-2), and anchoring missed 1,735 of them.
# The LAST match wins, so a name that happens to look like a number
# ('ADR-02-Searcher-EX2-046') still resolves to the real one at the tail.
DIGIMON_ID = re.compile(r"(?P<set>[A-Z]{1,4}\d*)-(?P<num>\d{2,3})(?![\d])")

# Slugs that are not game pieces at all.
IGNORE_TOKENS = ("-Token", "Token-", "-Oversized", "-Field-Center", "-Playmat", "-Sleeves")


class ResolutionReport:
    def __init__(self):
        self.counts = defaultdict(int)
        self.total = 0

    def record(self, status: str):
        self.counts[status] += 1
        self.total += 1

    @property
    def resolved(self) -> int:
        return (
            self.counts[Printing.Resolution.AUTO_EXACT]
            + self.counts[Printing.Resolution.AUTO_FUZZY]
        )

    @property
    def rate(self) -> float:
        """Share of the rows THIS run examined that ended up linked.

        On a --redo run that is the retry batch, not the collection: re-running
        against 234 known-hard leftovers and matching none of them is 0%, while
        the game as a whole stays at 99.6%. Callers that report to a human
        should show game_stats() alongside this, never this alone.
        """
        eligible = self.total - self.counts[Printing.Resolution.IGNORED]
        return (100.0 * self.resolved / eligible) if eligible else 0.0

    def summary(self) -> str:
        parts = ", ".join(f"{status}={count}" for status, count in sorted(self.counts.items()))
        return f"{self.total} printing(s) examined — {parts}"


def digimon_external_id(slug: str) -> str | None:
    """'A-Delicate-Plan-BT3-097-U' → 'BT3-097'; 'Agumon-BT11-046-V2' → 'BT11-046'."""
    matches = list(DIGIMON_ID.finditer(slug))
    if not matches:
        return None
    last = matches[-1]
    return f"{last.group('set')}-{last.group('num')}"


def ygo_candidate_slugs(slug: str) -> list[str]:
    """Slug forms to try, most literal first.

    Rarity and art-variant suffixes stack in either order and more than once
    ('A-Team-Trap-Disposal-Unit-V-1-Rare'), so peel them alternately until the
    slug stops shrinking, keeping every intermediate form as a candidate.

    Order is the safety property: a trap card whose real name ends in a rarity
    word (Junk-Collector, Elemental-HERO-Captain-Gold) matches on the untouched
    slug long before anything gets stripped.
    """
    candidates = [slug]
    current = slug
    for _ in range(6):  # bounded: real slugs never stack more than a few
        peeled = strip_version_suffix(strip_rarity_suffix(current))
        if peeled == current:
            peeled = strip_rarity_suffix(strip_version_suffix(current))
        if peeled == current or not peeled:
            break
        candidates.append(peeled)
        current = peeled
    # Last resort — a bare trailing number is an art variant, but it is also a
    # legitimate part of some card names, so it is only ever tried at the end.
    numberless = strip_number_suffix(current)
    if numberless and numberless != current:
        candidates.append(numberless)
    return candidates


def _is_ignorable(slug: str) -> bool:
    return any(token in slug for token in IGNORE_TOKENS)


def resolve_game(game, *, log=print, fuzzy: bool = True, redo: bool = False) -> ResolutionReport:
    """Resolve every unresolved printing of one game.

    Manual decisions are never overwritten — a catalog refresh must not silently
    undo a human's call. Pass redo=True to also retry previous no_match rows.
    """
    report = ResolutionReport()
    pieces = list(
        CardPiece.objects.filter(game=game).values(
            "id", "external_id", "normalized_name", "ambiguous_normalized"
        )
    )
    by_external = {piece["external_id"]: piece for piece in pieces}
    by_name: dict[str, list] = defaultdict(list)
    for piece in pieces:
        by_name[piece["normalized_name"]].append(piece)

    statuses = [Printing.Resolution.UNRESOLVED]
    if redo:
        statuses.append(Printing.Resolution.NO_MATCH)
    printings = Printing.objects.filter(
        expansion__game=game, resolution_status__in=statuses
    ).only("id", "slug")

    is_digimon = game.code == "digimon"
    updates = []
    for printing in printings.iterator(chunk_size=2000):
        piece_id, status, confidence = _resolve_one(
            printing.slug, by_external, by_name, is_digimon=is_digimon, fuzzy=fuzzy
        )
        report.record(status)
        printing.piece_id = piece_id
        printing.resolution_status = status
        printing.resolution_confidence = confidence
        updates.append(printing)
        if len(updates) >= 2000:
            _flush(updates)
    _flush(updates)
    log(report.summary())
    return report


def _flush(updates):
    if not updates:
        return
    with transaction.atomic():
        Printing.objects.bulk_update(
            updates, ["piece_id", "resolution_status", "resolution_confidence"]
        )
    updates.clear()


def _resolve_one(slug, by_external, by_name, *, is_digimon, fuzzy):
    if _is_ignorable(slug):
        return None, Printing.Resolution.IGNORED, None

    if is_digimon:
        external_id = digimon_external_id(slug)
        if external_id and external_id in by_external:
            return by_external[external_id]["id"], Printing.Resolution.AUTO_EXACT, 100.0
        return None, Printing.Resolution.NO_MATCH, None

    for candidate in ygo_candidate_slugs(slug):
        matches = by_name.get(normalize_name(candidate))
        if not matches:
            continue
        if len(matches) > 1 or matches[0]["ambiguous_normalized"]:
            # Several real cards share this name — a human decides.
            return None, Printing.Resolution.NO_MATCH, None
        return matches[0]["id"], Printing.Resolution.AUTO_EXACT, 100.0

    if not fuzzy:
        return None, Printing.Resolution.NO_MATCH, None
    return _fuzzy(slug, by_name)


def _fuzzy(slug, by_name):
    try:
        from rapidfuzz import process
    except ImportError:  # pragma: no cover - rapidfuzz is a declared dependency
        return None, Printing.Resolution.NO_MATCH, None

    target = normalize_name(strip_rarity_suffix(strip_version_suffix(slug)))
    if not target:
        return None, Printing.Resolution.NO_MATCH, None
    best = process.extractOne(target, by_name.keys(), score_cutoff=REVIEW_CUTOFF)
    if not best:
        return None, Printing.Resolution.NO_MATCH, None
    name, score, _ = best
    matches = by_name[name]
    if score >= AUTO_FUZZY_CUTOFF and len(matches) == 1 and not matches[0]["ambiguous_normalized"]:
        return matches[0]["id"], Printing.Resolution.AUTO_FUZZY, float(score)
    # Close but not certain: leave it for the review queue, keeping the score so
    # the queue can sort by how nearly it matched.
    return None, Printing.Resolution.NO_MATCH, float(score)


def game_stats(game) -> dict:
    """Where the whole game stands, independent of what a run just touched."""
    from django.db.models import Count

    counts = {
        row["resolution_status"]: row["n"]
        for row in Printing.objects.filter(expansion__game=game)
        .values("resolution_status").annotate(n=Count("id"))
    }
    total = sum(counts.values())
    ignored = counts.get(Printing.Resolution.IGNORED, 0)
    linked = (
        counts.get(Printing.Resolution.AUTO_EXACT, 0)
        + counts.get(Printing.Resolution.AUTO_FUZZY, 0)
        + counts.get(Printing.Resolution.MANUAL, 0)
    )
    eligible = total - ignored
    return {
        "total": total,
        "eligible": eligible,
        "linked": linked,
        "unmatched": counts.get(Printing.Resolution.NO_MATCH, 0),
        "rate": (100.0 * linked / eligible) if eligible else 0.0,
    }
