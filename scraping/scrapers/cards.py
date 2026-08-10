"""Card discovery within one expansion — port of the legacy get_cards().

Bug fix vs the notebook: the legacy code collected names (.col-10) and prices
(.col-price) as two independent lists zipped by index, so one row without a
price silently shifted every later price onto the wrong card. Here each TILE is
read as a unit.

Markup note (verified live 2026-08-09): Cardmarket replaced the singles table
with a card gallery. ``.table-body`` / ``.col-10`` / ``.col-price`` no longer
exist anywhere on the page. A tile is now::

    <a href="/en/YuGiOh/Products/Singles/<expansion>/<card>" class="… galleryBox">
      <img alt="Armory Arm" …>
      <div class="card-body …">
        <h2 class="card-title h3"><span class="expansion-symbol …"><span>DP08-JP</span></span>
            Armory Arm</h2>
        <p class="card-text h5"></p>
        <p class="card-text text-muted">From <b>6,00 €</b></p>
      </div>
    </a>
"""

import re

from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By

from ..browser import accept_cookies, navigate, random_delay, scroll_to_bottom
from ..errors import ScrapeError
from ..parsers import PriceParseError, parse_price

__all__ = ["ScrapeError", "discover_cards"]


_PAGES = re.compile(r"(\d+)(\+?)\s*$")
TILE = "a.galleryBox"
TILE_PRICE = "p.card-text.text-muted b"
# Cardmarket serves 30 tiles a page and stops paginating at 300 results, so a
# listing that reaches the last page is the "300+" cap the notebook worked around.
TILES_PER_PAGE = 30
MAX_LISTING_PAGES = 10


def _listing_url(segment: str, expansion_slug: str, page: int | None = None) -> str:
    base = f"https://www.cardmarket.com/en/{segment}/Products/Singles/{expansion_slug}"
    if page is not None:
        return f"{base}?idRarity=0&site={page}"
    return base


def _read_rows(driver, expansion_slug: str | None = None) -> list[tuple[str, object]]:
    """Return (card_slug, price_or_None) per tile of the current listing page."""
    results = []
    for tile in driver.find_elements(By.CSS_SELECTOR, TILE):
        href = (tile.get_attribute("href") or "").rstrip("/")
        parts = href.rsplit("/", 2)
        if len(parts) < 3:
            continue
        _, tile_expansion, slug = parts
        if not slug:
            continue
        # Tiles for other sets (recommendations, cross-links) must never be
        # filed under this expansion.
        if expansion_slug and tile_expansion.lower() != expansion_slug.lower():
            continue
        price = None
        try:
            price, _currency = parse_price(tile.find_element(By.CSS_SELECTOR, TILE_PRICE).text)
        except (NoSuchElementException, PriceParseError):
            pass  # price stays None for THIS tile only — no index shift
        results.append((slug, price))
    return results


def _is_empty_listing(driver) -> bool:
    """True when Cardmarket says this expansion has no products at all."""
    for element in driver.find_elements(By.CLASS_NAME, "noResults"):
        if element.text.strip():
            return True
    return False


def _page_count(driver) -> int:
    """Number of listing pages, from the "Page 1 of N" control.

    Fails CLOSED: a missing or unreadable pagination element means the page is
    not the listing we expect (bot block, redesign) — raising beats silently
    scraping one page and flagging the expansion fully scraped."""
    try:
        text = driver.find_element(By.ID, "pagination").text
    except NoSuchElementException:
        raise ScrapeError("pagination element not found — page layout unexpected") from None
    match = _PAGES.search(text.strip())
    if not match:
        raise ScrapeError(f"could not parse pagination text: {text!r}")
    return int(match.group(1))


def discover_cards(driver, segment: str, expansion_slug: str, log=print,
                   deep_search: bool = False) -> tuple[dict[str, object], bool]:
    """Scrape all card slugs (+ 'from' price) of one expansion's Singles listing.

    Returns ({card_slug: price_or_None}, complete). complete is False when the
    listing hit Cardmarket's 300-results cap and deep_search was off.
    """
    cards: dict[str, object] = {}

    navigate(driver, _listing_url(segment, expansion_slug), log=log)
    accept_cookies(driver)
    scroll_to_bottom(driver)
    random_delay(2, 4)

    if _is_empty_listing(driver):
        # A real, legitimately empty set (Cardmarket lists plenty of expansions
        # with no singles). The legacy notebook checked this too; without it the
        # missing pagination element reads as a bot block.
        log(f"{expansion_slug}: no products listed — nothing to import")
        return cards, True

    pages = _page_count(driver)
    for slug, price in _read_rows(driver, expansion_slug):
        cards.setdefault(slug, price)
    if not cards:
        raise ScrapeError("first listing page yielded zero cards — bot block or markup drift")

    for page in range(2, pages + 1):
        random_delay(3, 7)
        navigate(driver, _listing_url(segment, expansion_slug, page), log=log)
        scroll_to_bottom(driver)
        random_delay(1, 3)
        for slug, price in _read_rows(driver, expansion_slug):
            cards.setdefault(slug, price)
    # The pagination control no longer prints a "300+" marker; reaching the last
    # allowed page IS the cap, and everything past it is invisible to the listing.
    capped = pages >= MAX_LISTING_PAGES and len(cards) >= TILES_PER_PAGE * MAX_LISTING_PAGES
    log(f"{expansion_slug}: {len(cards)} cards over {pages} page(s)"
        + (" [capped at 300]" if capped else ""))

    if capped and deep_search:
        exhausted = _search_fallback(driver, segment, expansion_slug, cards, log)
        return cards, exhausted
    return cards, not capped


def _search_fallback(driver, segment, expansion_slug, cards, log) -> bool:
    """Port of the legacy 300+ workaround: search each known card name to
    surface variants hidden by the listing cap. Slow (6-10s per name).
    Returns True only when the queue was fully drained — a truncated crawl
    must NOT report the expansion as complete."""
    queue = list(cards)
    seen = set(cards)
    iterations = 0
    while queue and iterations < 1000:
        name = queue.pop(0)
        iterations += 1
        random_delay(6, 10)
        navigate(
            driver,
            f"https://www.cardmarket.com/en/{segment}/Products/Singles/"
            f"{expansion_slug}?searchString={name}",
            log=log,
        )
        for slug, price in _read_rows(driver, expansion_slug):
            if slug not in seen:
                seen.add(slug)
                cards[slug] = price
                queue.append(slug)
    exhausted = not queue
    log(f"{expansion_slug}: deep search finished, {len(cards)} cards total"
        + ("" if exhausted else " [TRUNCATED at 1000 searches — incomplete]"))
    return exhausted
