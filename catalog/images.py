"""Local card-art cache.

Both catalogs forbid hotlinking in the strongest terms — YGOPRODeck's guide
says "download and re-host the images yourself. Failure to do so will result in
an IP blacklist." So no template ever points at a remote image URL; they all go
through `card-art`, which downloads once, writes it under `var/card-images/`,
and serves the local copy forever after.

Lazy rather than bulk: the first person to look at a card pays ~200ms for it,
and a collection of 61k printings never triggers a multi-gigabyte download for
art nobody opens.
"""

import hashlib
import logging
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

logger = logging.getLogger(__name__)

SIZES = ("small", "cropped", "full")
# Only these hosts are ever fetched: the URL comes out of the database, and a
# poisoned catalog row must not turn this into a request-forgery gadget.
BUILTIN_HOSTS = frozenset({
    "images.ygoprodeck.com",
    "storage.googleapis.com",
    "images.digimoncard.io",
    "digimoncard.io",
})


def allowed_hosts() -> frozenset[str]:
    """Built-in providers, plus whatever hosts a configured custom provider
    legitimately uses. Derived from the game's own config rather than from the
    stored image URLs, so a bad row still can't widen the allowlist."""
    from .models import Game

    hosts = set(BUILTIN_HOSTS)
    for config in Game.objects.exclude(catalog_config={}).values_list(
        "catalog_config", flat=True
    ):
        for key in ("url", "image_url_template", "image_host"):
            value = config.get(key)
            if isinstance(value, str) and value:
                host = (urlparse(value).hostname or "").lower()
                if host:
                    hosts.add(host)
    return frozenset(hosts)
USER_AGENT = "Cardvault/1.0 (personal collection manager)"
TIMEOUT = 20
MAX_BYTES = 8 * 1024 * 1024


class ImageUnavailable(Exception):
    """No art for this piece, or the fetch failed. Callers show a placeholder."""


def cache_root() -> Path:
    path = Path(settings.CARD_IMAGE_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def cached_path(piece, size: str) -> Path:
    """Sharded by a hash of the id so no directory holds 13k files."""
    key = f"{piece.game_id}-{piece.external_id}-{size}"
    digest = hashlib.sha256(key.encode()).hexdigest()
    return cache_root() / digest[:2] / f"{digest}.jpg"


def ensure_cached(piece, size: str = "small") -> Path:
    """Return a local path to this piece's art, downloading it if needed."""
    if size not in SIZES:
        raise ImageUnavailable(f"unknown size {size!r}")
    path = cached_path(piece, size)
    if path.exists() and path.stat().st_size:
        return path

    url = piece.remote_image(size)
    if not url:
        raise ImageUnavailable(f"{piece.name}: no {size} image on record")
    host = (urlparse(url).hostname or "").lower()
    if host not in allowed_hosts():
        raise ImageUnavailable(f"refusing to fetch art from unexpected host {host!r}")

    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp name first: a half-downloaded file must never be served,
    # and two requests for the same cold image must not interleave.
    temp = path.with_suffix(f".{hashlib.md5(url.encode()).hexdigest()[:8]}.part")
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise ImageUnavailable(f"{url} returned HTTP {response.status}")
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ImageUnavailable(f"{url} is larger than {MAX_BYTES} bytes")
        if not data:
            raise ImageUnavailable(f"{url} returned an empty body")
        temp.write_bytes(data)
        shutil.move(str(temp), str(path))
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        temp.unlink(missing_ok=True)
        raise ImageUnavailable(f"could not fetch {url}: {exc}") from exc
    logger.info("cached art for %s (%s), %d bytes", piece.name, size, len(data))
    return path


def cache_stats() -> dict:
    root = cache_root()
    files = [p for p in root.rglob("*.jpg") if p.is_file()]
    return {"count": len(files), "bytes": sum(p.stat().st_size for p in files)}

