"""External card catalogs → CardPiece rows.

Two providers, one shape:

* **YGOPRODeck** (`db.ygoprodeck.com/api/v7`) — one `cardinfo.php` call returns
  the whole ~13k-card database, so we never approach the documented 20 req/s
  limit. `checkDBVer.php` lets a re-import become a no-op.
* **digimoncard.io** (`getAllCards.php`) — likewise one call. Its `id` field is
  the set number ("ST1-03"), which IS the Digimon card's identity; art lives at
  a predictable `images.digimoncard.io` path (verified 2026-08-09).

Both are read-only, unauthenticated, and rate-limit friendly at one request per
import. Prices from YGOPRODeck are informational — Cardmarket scraping remains
the authority for what your collection is worth.
"""

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from .models import CardPiece, CardPieceAlias
from .normalize import normalize_name

logger = logging.getLogger(__name__)

YGOPRODECK_CARDS = "https://db.ygoprodeck.com/api/v7/cardinfo.php"
YGOPRODECK_VERSION = "https://db.ygoprodeck.com/api/v7/checkDBVer.php"
# NOT getAllCards.php — that returns only {name, cardnumber}. search.php with a
# series filter is the bulk endpoint that carries the full 33-field records.
DIGIMON_CARDS = "https://digimoncard.io/api-public/search.php"
DIGIMON_IMAGE = "https://images.digimoncard.io/images/cards/{card_id}.jpg"
USER_AGENT = "Cardvault/1.0 (personal collection manager)"
TIMEOUT = 120

EXTRA_DECK_TYPES = ("fusion", "synchro", "xyz", "link")


class CatalogError(RuntimeError):
    """The provider was unreachable or returned something unusable."""


@dataclass
class ImportResult:
    created: int = 0
    updated: int = 0
    aliases: int = 0
    skipped: int = 0
    ambiguous: int = 0
    version: str = ""
    notes: list = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.created} new, {self.updated} updated, {self.aliases} aliases, "
            f"{self.ambiguous} ambiguous names, {self.skipped} skipped"
        )


def _fetch_json(url: str, *, params: dict | None = None):
    if params:
        from urllib.parse import urlencode
        url = f"{url}?{urlencode(params)}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise CatalogError(f"{url} unreachable: {exc}") from exc
    try:
        return json.loads(payload)
    except ValueError as exc:
        raise CatalogError(f"{url} returned invalid JSON: {exc}") from exc


def _decimal(value):
    if value in (None, "", "0", 0, "0.00"):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    return amount.quantize(Decimal("0.01")) if amount.is_finite() and amount > 0 else None


def _int(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number


def ygoprodeck_version() -> str:
    payload = _fetch_json(YGOPRODECK_VERSION)
    if isinstance(payload, list) and payload:
        return str(payload[0].get("database_version", ""))
    return ""


# -- YuGiOh -------------------------------------------------------------------

def import_ygoprodeck(game, *, log=print, limit: int | None = None) -> ImportResult:
    result = ImportResult()
    try:
        result.version = ygoprodeck_version()
    except CatalogError as exc:  # version check is a nicety, not a gate
        result.notes.append(f"version check failed: {exc}")
    log("downloading the YGOPRODeck card database (one request, ~13k cards)…")
    payload = _fetch_json(YGOPRODECK_CARDS, params={"misc": "yes"})
    cards = payload.get("data") if isinstance(payload, dict) else None
    if not cards:
        raise CatalogError("YGOPRODeck returned no card data")
    if limit:
        cards = cards[:limit]
    log(f"{len(cards)} cards received; writing pieces…")
    return _store(game, (_ygo_piece(card) for card in cards), result, log=log)


def _ygo_piece(card: dict) -> dict:
    images = card.get("card_images") or []
    prices = (card.get("card_prices") or [{}])[0]
    misc = (card.get("misc_info") or [{}])[0]
    banlist = card.get("banlist_info") or {}
    frame = (card.get("frameType") or "").lower()
    first_image = images[0] if images else {}
    return {
        "external_id": str(card.get("id", "")),
        "konami_id": str(misc.get("konami_id") or ""),
        "name": card.get("name") or "",
        "card_type": card.get("type") or "",
        "frame_type": card.get("frameType") or "",
        "race": card.get("race") or "",
        "attribute": card.get("attribute") or "",
        "archetype": card.get("archetype") or "",
        "level": _int(card.get("level")),
        "atk": _int(card.get("atk")),
        "defence": _int(card.get("def")),
        "text": card.get("desc") or "",
        "ban_tcg": banlist.get("ban_tcg") or "",
        "ban_ocg": banlist.get("ban_ocg") or "",
        "is_extra_deck": any(token in frame for token in EXTRA_DECK_TYPES),
        "image_url": first_image.get("image_url") or "",
        "image_small_url": first_image.get("image_url_small") or "",
        "image_cropped_url": first_image.get("image_url_cropped") or "",
        "catalog_price_eur": _decimal(prices.get("cardmarket_price")),
        "extra": {
            "linkval": card.get("linkval"),
            "linkmarkers": card.get("linkmarkers"),
            "scale": card.get("scale"),
            "ygoprodeck_url": card.get("ygoprodeck_url"),
            "tcg_date": misc.get("tcg_date"),
            "set_codes": sorted({
                entry.get("set_code", "") for entry in (card.get("card_sets") or [])
                if entry.get("set_code")
            })[:12],
        },
        # Alt-art passcodes: .ydk decklists reference these, not the base id.
        "aliases": [str(image["id"]) for image in images if image.get("id")],
    }


# -- Digimon ------------------------------------------------------------------

def import_digimoncard(game, *, log=print, limit: int | None = None) -> ImportResult:
    result = ImportResult()
    log("downloading the digimoncard.io card list (one request)…")
    cards = _fetch_json(DIGIMON_CARDS, params={"sort": "name", "series": "Digimon Card Game"})
    if not isinstance(cards, list) or not cards:
        raise CatalogError("digimoncard.io returned no card data")
    # The feed lists a row per set appearance, so the same card number recurs;
    # collapse to one piece per number before writing.
    unique: dict[str, dict] = {}
    for card in cards:
        card_id = (card.get("id") or "").strip()
        if card_id:
            unique.setdefault(card_id, card)
    cards = list(unique.values())
    if limit:
        cards = cards[:limit]
    log(f"{len(cards)} distinct cards received; writing pieces…")
    return _store(game, (_digimon_piece(card) for card in cards), result, log=log)


def _digimon_piece(card: dict) -> dict:
    card_id = (card.get("id") or "").strip()
    digi_types = [card.get(f"digi_type{n}") for n in ("", "2", "3", "4", "5")]
    image = DIGIMON_IMAGE.format(card_id=card_id) if card_id else ""
    return {
        "external_id": card_id,
        "name": card.get("name") or "",
        "card_type": card.get("type") or "",
        "race": card.get("digi_type") or "",
        "attribute": card.get("attribute") or "",
        "colour": card.get("color") or "",
        "level": _int(card.get("level")),
        "atk": _int(card.get("dp")),
        "play_cost": _int(card.get("play_cost")),
        "evolution_cost": _int(card.get("evolution_cost")),
        "text": "\n\n".join(
            part for part in (card.get("main_effect"), card.get("source_effect")) if part
        ),
        "image_url": image,
        "image_small_url": image,
        "image_cropped_url": "",
        "extra": {
            "form": card.get("form"),
            "stage": card.get("stage"),
            "rarity": card.get("rarity"),
            "artist": card.get("artist"),
            "digi_types": [t for t in digi_types if t],
            "set_names": card.get("set_name") or [],
            "pretty_url": card.get("pretty_url"),
        },
        "aliases": [],
    }


# -- shared write path --------------------------------------------------------

def _store(game, pieces, result: ImportResult, *, log=print) -> ImportResult:
    """Upsert pieces in one transaction, then flag colliding normalized names.

    Never touches Printing.piece — resolution is a separate, auditable step, and
    a catalog refresh must not silently re-link a manual decision.
    """
    now = timezone.now()
    seen_normalized: dict[str, int] = {}
    with transaction.atomic():
        for data in pieces:
            aliases = data.pop("aliases", [])
            external_id = data.get("external_id")
            if not external_id or not data.get("name"):
                result.skipped += 1
                continue
            data["normalized_name"] = normalize_name(data["name"])
            data["catalog_updated_at"] = now
            seen_normalized[data["normalized_name"]] = (
                seen_normalized.get(data["normalized_name"], 0) + 1
            )
            piece, created = CardPiece.objects.update_or_create(
                game=game, external_id=external_id, defaults=data
            )
            result.created += created
            result.updated += not created
            for alias in aliases:
                if alias == external_id:
                    continue
                _, made = CardPieceAlias.objects.get_or_create(
                    piece=piece, alias_passcode=alias
                )
                result.aliases += made

        # Collisions are computed from the DATABASE, not just this payload: a
        # name that appears once in the feed can still collide with a piece
        # imported earlier, and auto-resolution must never pick between them.
        collisions = list(
            CardPiece.objects.filter(game=game)
            .values_list("normalized_name", flat=True)
            .annotate(n=Count("id"))
            .filter(n__gt=1)
        )
        if collisions:
            result.ambiguous = CardPiece.objects.filter(
                game=game, normalized_name__in=collisions
            ).update(ambiguous_normalized=True)
        CardPiece.objects.filter(game=game, ambiguous_normalized=True).exclude(
            normalized_name__in=collisions
        ).update(ambiguous_normalized=False)
    log(result.summary())
    return result


# -- generic, configured provider --------------------------------------------

# Which CardPiece fields a custom mapping may set, and how to coerce each.
MAPPABLE = {
    "external_id": str, "name": str, "konami_id": str,
    "card_type": str, "frame_type": str, "race": str, "attribute": str,
    "colour": str, "archetype": str, "text": str,
    "ban_tcg": str, "ban_ocg": str,
    "level": _int, "atk": _int, "defence": _int,
    "play_cost": _int, "evolution_cost": _int,
    "catalog_price_eur": _decimal,
    "image_url": str, "image_small_url": str, "image_cropped_url": str,
}


def _dig(data, path: str):
    """Follow a dotted path into nested dicts/lists: 'images.0.large'."""
    value = data
    for part in str(path).split("."):
        if value is None:
            return None
        if isinstance(value, list):
            try:
                value = value[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def import_custom(game, *, log=print, limit: int | None = None) -> ImportResult:
    """Import from any JSON API describable in Game.catalog_config.

    Enough shape for the common case — one request returning a list of card
    objects — so a new game is a form to fill in rather than a code change.
    Anything stranger still needs a real importer function.
    """
    config = game.catalog_config or {}
    url = config.get("url")
    if not url:
        raise CatalogError(
            f"{game.name} uses the custom provider but no URL is configured. "
            "Set catalog_config on the game in Records."
        )
    fields = config.get("fields") or {}
    if "external_id" not in fields or "name" not in fields:
        raise CatalogError(
            'catalog_config["fields"] must map at least "external_id" and "name" '
            "to keys in the API response."
        )

    log(f"downloading {url}…")
    payload = _fetch_json(url, params=config.get("params") or None)
    rows = _dig(payload, config["results_path"]) if config.get("results_path") else payload
    if not isinstance(rows, list) or not rows:
        raise CatalogError(
            f"{url} did not return a list of cards"
            + (f' at "{config["results_path"]}"' if config.get("results_path") else "")
        )
    if limit:
        rows = rows[:limit]
    log(f"{len(rows)} rows received; mapping…")

    template = config.get("image_url_template", "")
    extra_keys = config.get("extra") or []

    def build(row):
        data = {}
        for target, source in fields.items():
            if target not in MAPPABLE:
                continue
            raw = _dig(row, source)
            coerce = MAPPABLE[target]
            data[target] = (
                coerce(raw) if coerce is not str
                else ("" if raw is None else str(raw).strip())
            )
        data.setdefault("external_id", "")
        if template and not data.get("image_url"):
            data["image_url"] = template.format(**{
                k: v for k, v in data.items() if isinstance(v, str)
            })
        data.setdefault("image_small_url", data.get("image_url", ""))
        data["extra"] = {key: _dig(row, key) for key in extra_keys}
        data["aliases"] = []
        return data

    seen, unique = set(), []
    for row in rows:
        piece = build(row)
        if piece["external_id"] and piece["external_id"] not in seen:
            seen.add(piece["external_id"])
            unique.append(piece)
    return _store(game, iter(unique), ImportResult(), log=log)


# Built-in providers. A game names one on its own record, so adding a game
# never means editing this file.
IMPORTERS = {
    "ygoprodeck": import_ygoprodeck,
    "digimoncard": import_digimoncard,
    "custom": import_custom,
}


def importer_for(game):
    """The import function for a game, or None if it has no catalog."""
    return IMPORTERS.get(game.catalog_provider)
