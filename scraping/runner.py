"""Job runner — executed by `manage.py run_scrape_job <id>` in its own process.

Checkpoint granularity is ONE item = one transaction (snapshot + denormalized
prices + item status), so a crash or cancel loses nothing already fetched —
this replaces the legacy JSON checkpoint files.
"""

import os
import time
import traceback
from datetime import datetime

from django.db import transaction
from django.utils import timezone
from selenium.common.exceptions import InvalidSessionIdException, WebDriverException

from catalog.models import Expansion, Printing
from catalog.normalize import normalize_name, slug_display_name
from pricing.models import PriceSnapshot

from .browser import build_driver, home_url, random_delay
from .errors import ScrapeBlocked
from .models import ScrapeJob, ScrapeJobItem, ScraperSettings
from .scrapers.cards import discover_cards
from .scrapers.expansions import discover_expansions
from .scrapers.prices import scrape_card_prices


class JobCancelled(Exception):
    pass


# Each successive wait is longer: a check that didn't clear in 3 minutes is
# unlikely to clear 30 seconds later, but very often has by the time you've
# left it alone for a quarter of an hour.
CHALLENGE_BACKOFF = (60, 300, 900, 1800)


def _wait_out_challenge(job, attempt: int, exc, log) -> None:
    """Pause after a challenge that didn't clear, checking for cancellation.

    The alternative — aborting — throws away a run that is usually one patient
    pause away from continuing. A refusal is different and never gets here.
    """
    pause = CHALLENGE_BACKOFF[min(attempt - 1, len(CHALLENGE_BACKOFF) - 1)]
    log(f"challenge didn't clear (attempt {attempt}) — pausing {pause // 60 or 1} min, "
        "then starting a fresh browser and carrying on")
    ScrapeJob.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now())
    waited = 0
    while waited < pause:
        _check_cancel(job)          # cancelling during a long pause must work
        time.sleep(min(10, pause - waited))
        waited += 10
        ScrapeJob.objects.filter(pk=job.pk).update(heartbeat_at=timezone.now())


def run_job(job_id: int) -> None:
    job = ScrapeJob.objects.get(pk=job_id)
    log_fh = open(job.log_path, "a", buffering=1) if job.log_path else None

    def log(message: str) -> None:
        line = f"{datetime.now():%H:%M:%S} {message}"
        if log_fh:
            log_fh.write(line + "\n")
        else:
            print(line)

    # Everything after the log handle opens — backup, claim, work — runs under
    # the same error handling: any failure lands the job in FAILED instead of
    # stranding it in an active status, and the handle always closes.
    try:
        claimed = ScrapeJob.objects.filter(
            pk=job.pk, status=ScrapeJob.Status.PENDING
        ).update(
            status=ScrapeJob.Status.BACKING_UP,
            pid=os.getpid(),
            heartbeat_at=timezone.now(),
        )
        if not claimed:
            log(f"job #{job.pk} is not pending (cancelled before start?) — aborting")
            return

        _pre_job_backup(job, log)

        started = ScrapeJob.objects.filter(
            pk=job.pk, status=ScrapeJob.Status.BACKING_UP
        ).update(
            status=ScrapeJob.Status.RUNNING,
            started_at=timezone.now(),
            heartbeat_at=timezone.now(),
        )
        if not started:
            log("job was cancelled during the backup — aborting cleanly")
            return

        job.refresh_from_db()
        handler = {
            ScrapeJob.Type.DISCOVER_EXPANSIONS: _run_discover_expansions,
            ScrapeJob.Type.DISCOVER_CARDS: _run_discover_cards,
            ScrapeJob.Type.REFRESH_PRICES: _run_refresh_prices,
            ScrapeJob.Type.IMPORT_CATALOG: _run_import_catalog,
            ScrapeJob.Type.RESOLVE_PRINTINGS: _run_resolve_printings,
            ScrapeJob.Type.CACHE_ART: _run_cache_art,
            ScrapeJob.Type.API_PRICES: _run_api_prices,
        }[ScrapeJob.Type(job.job_type)]
        handler(job, log)
    except JobCancelled:
        skipped = _close_out_items(job, "not attempted — job cancelled")
        _finish(job, ScrapeJob.Status.CANCELLED)
        log(f"job cancelled — everything already fetched is saved; "
            f"{skipped} item(s) were never attempted. Run the job again to cover them.")
    except ScrapeBlocked as exc:
        # Bot protection is in the way: every remaining item would fail the same
        # way. Stop with the advice, not a traceback — this is an operational
        # condition, not a bug. Everything already fetched is committed.
        _close_out_items(job, "not attempted — job stopped by bot protection")
        ScrapeJob.objects.filter(pk=job.pk).update(
            status=ScrapeJob.Status.FAILED, error=str(exc), finished_at=timezone.now()
        )
        log(f"job STOPPED — {exc}")
    except Exception:
        _close_out_items(job, "not attempted — the job failed first")
        ScrapeJob.objects.filter(pk=job.pk).update(
            status=ScrapeJob.Status.FAILED,
            error=traceback.format_exc(),
            finished_at=timezone.now(),
        )
        log(f"job FAILED:\n{traceback.format_exc()}")
    else:
        job.refresh_from_db()
        if job.status == ScrapeJob.Status.RUNNING:
            status = (
                ScrapeJob.Status.COMPLETED_WITH_ERRORS
                if job.failed_items
                else ScrapeJob.Status.COMPLETED
            )
            _finish(job, status)
            log(f"job finished: {job.processed_items} done, {job.failed_items} failed")
    finally:
        if log_fh:
            log_fh.close()


def _pre_job_backup(job, log) -> None:
    """The mandatory pre-job backup, taken by the runner itself so the web
    request never holds SQLite's write lock across a full-database copy."""
    from django.core.management import call_command

    log(f"taking pre-job backup (tag pre-job-{job.pk})…")
    call_command("backup_db", tag=f"pre-job-{job.pk}")
    log("backup complete")


def _finish(job, status, *, reason: str = ""):
    _close_out_items(job, reason)
    ScrapeJob.objects.filter(pk=job.pk).update(status=status, finished_at=timezone.now())


def _close_out_items(job, reason: str = "") -> int:
    """Mark whatever was never reached as SKIPPED.

    A job is finished for good once it reaches a terminal status — there is no
    resume. Leaving the remainder as PENDING made a cancelled run look like it
    was still half-done and could be picked up; it cannot. To cover the rest,
    start the same job again: finished work is excluded by scope, so the new run
    only does what is genuinely outstanding.
    """
    return ScrapeJobItem.objects.filter(
        job=job, status=ScrapeJobItem.Status.PENDING
    ).update(
        status=ScrapeJobItem.Status.SKIPPED,
        message=(reason or "not attempted — the job ended first")[:250],
        processed_at=timezone.now(),
    )


def _check_cancel(job) -> None:
    status = ScrapeJob.objects.values_list("status", flat=True).get(pk=job.pk)
    if status == ScrapeJob.Status.CANCEL_REQUESTED:
        raise JobCancelled


def _tick(job, *, failed: bool = False) -> None:
    updates = {"heartbeat_at": timezone.now()}
    job.processed_items += 1
    updates["processed_items"] = job.processed_items
    if failed:
        job.failed_items += 1
        updates["failed_items"] = job.failed_items
    ScrapeJob.objects.filter(pk=job.pk).update(**updates)


# -- discover expansions ------------------------------------------------------

def _run_discover_expansions(job, log):
    segment = job.game.cardmarket_segment
    last_n_years = int(job.params.get("last_n_years", 2))
    driver = build_driver(
        headless=bool(job.params.get("headless", False)),
        warm_url=home_url(segment), log=log,
    )
    try:
        slugs = discover_expansions(driver, segment, last_n_years, log=log)
    finally:
        driver.quit()

    ScrapeJob.objects.filter(pk=job.pk).update(total_items=len(slugs))
    job.total_items = len(slugs)
    now = timezone.now()
    created = 0
    for slug in slugs:
        _check_cancel(job)
        _, was_created = Expansion.objects.update_or_create(
            game=job.game, slug=slug,
            defaults={"last_discovered_at": now},
            create_defaults={
                "display_name": slug_display_name(slug),
                "last_discovered_at": now,
            },
        )
        created += was_created
        _tick(job)
    log(f"{len(slugs)} expansions found, {created} new")


# -- discover cards -----------------------------------------------------------

def _run_discover_cards(job, log):
    segment = job.game.cardmarket_segment
    deep_search = bool(job.params.get("deep_search", False))
    headless = bool(job.params.get("headless", False))
    recycle_every = int(job.params.get("recycle_every", 25))
    items = list(
        job.items.filter(status=ScrapeJobItem.Status.PENDING, expansion__isnull=False)
        .select_related("expansion")
    )
    # ONE browser for the run. The legacy notebook rebuilt the driver per
    # expansion; behind Cloudflare that discards the clearance cookie and
    # re-triggers the check on every set. Recycling still happens, just rarely,
    # and the persistent profile carries the clearance across the restart.
    pace = ScraperSettings.load()
    driver = build_driver(headless=headless, warm_url=home_url(segment), log=log)
    since_recycle = 0
    challenge_attempt = 0
    retry_queue = []
    try:
        # Items pushed back by a challenge are re-queued at the end rather than
        # lost, so a pause costs time and nothing else.
        index = 0
        while index < len(items) or retry_queue:
            item = items[index] if index < len(items) else retry_queue.pop(0)
            index += 1
            _check_cancel(job)
            expansion = item.expansion
            if since_recycle >= recycle_every:
                driver.quit()
                driver = build_driver(headless=headless, warm_url=home_url(segment), log=log)
                since_recycle = 0
            if index > 1:
                # Pace between sets, from the operator settings — bursts get us blocked.
                random_delay(pace.min_item_delay, pace.max_item_delay)
            try:
                cards, complete = discover_cards(
                    driver, segment, expansion.slug, log=log, deep_search=deep_search
                )
            except ScrapeBlocked as exc:
                if not exc.recoverable or challenge_attempt >= pace.challenge_attempts:
                    raise  # a refusal, or we have been patient enough
                challenge_attempt += 1
                _wait_out_challenge(job, challenge_attempt, exc, log)
                driver.quit()
                driver = build_driver(headless=headless, warm_url=home_url(segment), log=log)
                since_recycle = 0
                retry_queue.append(item)
                continue
            except Exception as exc:
                item.status = ScrapeJobItem.Status.FAILED
                item.message = str(exc)[:250]
                item.processed_at = timezone.now()
                item.save()
                _tick(job, failed=True)
                log(f"{expansion.slug}: FAILED — {exc}")
                continue
            since_recycle += 1
            challenge_attempt = 0     # a success clears the patience budget
            _save_discovered_cards(job, item, expansion, cards, complete, log)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _save_discovered_cards(job, item, expansion, cards, complete, log):
    """One expansion = one transaction (checkpoint granularity)."""
    with transaction.atomic():
        existing = set(expansion.printings.values_list("slug", flat=True))
        now = timezone.now()
        new_printings = [
            Printing(
                expansion=expansion,
                slug=slug,
                display_name=slug_display_name(slug),
                name_normalized=normalize_name(slug),
                current_price_from=price,
                prices_updated_at=now if price is not None else None,
            )
            for slug, price in cards.items()
            if slug not in existing
        ]
        Printing.objects.bulk_create(new_printings)
        expansion.fully_scraped = complete
        expansion.last_discovered_at = now
        expansion.save(update_fields=["fully_scraped", "last_discovered_at", "updated_at"])
        item.status = ScrapeJobItem.Status.DONE
        item.message = (
            f"{len(new_printings)} new of {len(cards)}"
            + ("" if complete else " [capped — enable deep search]")
        )
        item.processed_at = timezone.now()
        item.save()
    _tick(job)
    log(f"{expansion.slug}: {len(new_printings)} new cards ({len(cards)} listed)")


# -- refresh prices -----------------------------------------------------------

_DRIVER_ERRORS = (WebDriverException, InvalidSessionIdException)


def _run_refresh_prices(job, log):
    recycle_every = int(job.params.get("recycle_every", 20))
    headless = bool(job.params.get("headless", False))
    items = list(
        job.items.filter(status=ScrapeJobItem.Status.PENDING, card__isnull=False)
        .select_related("card", "card__expansion", "card__expansion__game")
    )
    warm = home_url(job.game.cardmarket_segment)
    pace = ScraperSettings.load()
    driver = build_driver(headless=headless, warm_url=warm, log=log)
    since_recycle = 0
    challenge_attempt = 0
    retry_queue = []
    try:
        index = 0
        while index < len(items) or retry_queue:
            item = items[index] if index < len(items) else retry_queue.pop(0)
            index += 1
            _check_cancel(job)
            if since_recycle >= recycle_every:
                driver.quit()
                driver = build_driver(headless=headless, warm_url=warm, log=log)
                since_recycle = 0
            if index > 1:
                # Pace between cards, from the operator settings. This is the job
                # that hits Cardmarket hardest — hundreds of product pages in a
                # row — so it is the one that most needs to be slowed down.
                random_delay(pace.min_item_delay, pace.max_item_delay)

            card = item.card
            prices: dict = {}
            last_error = None
            for attempt in range(1, 4):
                try:
                    prices = scrape_card_prices(driver, card.cardmarket_url, log=log)
                    if prices:
                        break
                    last_error = "no price labels found"
                except ScrapeBlocked as exc:
                    # A refusal is fatal; a check that merely ran long is not.
                    if not exc.recoverable or challenge_attempt >= pace.challenge_attempts:
                        raise
                    challenge_attempt += 1
                    _wait_out_challenge(job, challenge_attempt, exc, log)
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = build_driver(headless=headless, warm_url=warm, log=log)
                    since_recycle = 0
                    retry_queue.append(item)
                    prices, last_error = {}, "requeued after a Cloudflare pause"
                    break
                except _DRIVER_ERRORS as exc:
                    # Session is likely dead — rebuild before the next attempt.
                    last_error = f"webdriver: {type(exc).__name__}"
                    log(f"attempt {attempt}/3 failed on {card.slug}: {last_error}")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = build_driver(headless=headless, warm_url=warm, log=log)
                    since_recycle = 0
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    log(f"attempt {attempt}/3 failed on {card.slug}: {last_error}")

            if last_error == "requeued after a Cloudflare pause":
                continue  # not a failure: the card goes back in the queue
            if not prices:
                item.status = ScrapeJobItem.Status.FAILED
                item.message = (last_error or "unknown")[:250]
                item.processed_at = timezone.now()
                item.save()
                _tick(job, failed=True)
                log(f"{card.slug}: FAILED — {last_error}")
                continue

            problems = _store_prices(job, card, prices)
            item.status = ScrapeJobItem.Status.DONE
            item.message = ("partial: " + ", ".join(problems))[:250] if problems else ""
            item.processed_at = timezone.now()
            item.save()
            _tick(job)
            since_recycle += 1
            challenge_attempt = 0     # a success clears the patience budget
            log(f"{card.slug}: " + ", ".join(
                f"{k}={v}" for k, v in prices.items() if k != "currency"
            ))
    finally:
        try:
            driver.quit()
        except Exception:
            pass


PRICE_FIELDS = ("price_from", "price_trend", "price_30d_avg")


def _store_prices(job, card, prices) -> list[str]:
    """Snapshot + denormalized current prices in ONE transaction.

    Returns the list of missing price labels. Two honesty rules:
    - Denormalized current_price_* only accepts EUR — a GBP/USD page must not
      silently mix currencies into the valuation totals (the snapshot always
      records the real currency).
    - prices_updated_at is only bumped when ALL THREE labels parsed, so a
      partial scrape stays 'stale' and gets retried by the next stale-only run.
    """
    observed = timezone.now()
    missing = [f for f in PRICE_FIELDS if f not in prices]
    currency = prices.get("currency", "EUR")
    with transaction.atomic():
        PriceSnapshot.objects.create(
            card=card,
            price_from=prices.get("price_from"),
            price_trend=prices.get("price_trend"),
            price_30d_avg=prices.get("price_30d_avg"),
            currency=currency,
            observed_at=observed,
            source=PriceSnapshot.Source.CARD_PAGE,
            scrape_job=job,
        )
        if currency != "EUR":
            return missing + [f"non-EUR price ({currency}) — denormalized fields not updated"]
        update_fields = ["updated_at", "price_source"]
        card.price_source = Printing.PriceSource.SCRAPE
        if "price_from" in prices:
            card.current_price_from = prices["price_from"]
            update_fields.append("current_price_from")
        if "price_trend" in prices:
            card.current_price_trend = prices["price_trend"]
            update_fields.append("current_price_trend")
        if "price_30d_avg" in prices:
            card.current_price_30d = prices["price_30d_avg"]
            update_fields.append("current_price_30d")
        if not missing:
            card.prices_updated_at = observed
            update_fields.append("prices_updated_at")
        card.save(update_fields=update_fields)
    return missing


# -- public-API jobs ----------------------------------------------------------
# These never open a browser, so nothing here can be blocked by Cloudflare or
# broken by a Chrome path. They are the "update everything without the scraper"
# half of the app.

def _run_import_catalog(job, log):
    """Pull card metadata + art URLs from the game's public catalog API."""
    from catalog.catalogs import CatalogError, importer_for

    importer = importer_for(job.game)
    if importer is None:
        raise RuntimeError(
            f"{job.game.name} has no card catalog configured. Open Records → Games → "
            f"{job.game.name} and set “Catalog provider”. Yu-Gi-Oh! and Digimon have "
            "built-in providers; anything else can use “Custom JSON API” with a URL "
            "and a field mapping — no code needed."
        )
    try:
        result = importer(job.game, log=log)
    except CatalogError as exc:
        raise RuntimeError(str(exc)) from exc

    ScrapeJob.objects.filter(pk=job.pk).update(
        total_items=result.created + result.updated,
        processed_items=result.created + result.updated,
    )
    log(f"catalog import finished: {result.summary()}")
    if result.version:
        log(f"provider database version: {result.version}")


def _run_resolve_printings(job, log):
    """Link Cardmarket printings to catalog cards — the join that gives a
    printing its art, type, attribute and banlist status."""
    from catalog.resolution import game_stats, resolve_game

    if not job.game.pieces.exists():
        raise RuntimeError(
            f"No catalog imported for {job.game.name} yet — run “import card catalog” first."
        )
    report = resolve_game(
        job.game, log=log,
        fuzzy=bool(job.params.get("fuzzy", True)),
        redo=bool(job.params.get("redo", False)),
    )
    # A printing the catalog simply does not contain — an OCG-only card, a
    # localized name, a Speed Duel Skill — is not a job failure. Counting it as
    # one made a successful retry read "234 done, 234 failed".
    ScrapeJob.objects.filter(pk=job.pk).update(
        total_items=report.total, processed_items=report.total, failed_items=0,
    )
    newly = report.resolved
    log(f"{newly} newly linked out of {report.total} examined")
    stats = game_stats(job.game)
    log(f"{job.game.name} overall: {stats['linked']:,} of {stats['eligible']:,} printings "
        f"linked ({stats['rate']:.1f}%), {stats['unmatched']:,} with no catalog match")
    if report.total and not newly:
        log("Nothing new matched. What is left is usually cards the catalog does not have "
            "under that name — OCG-only printings, localized names, Speed Duel Skills. "
            "Link the ones you own by hand in the admin, or mark them ignored.")


def _run_cache_art(job, log):
    """Pre-download card art so browsing is instant and offline-safe.

    Art is normally fetched lazily on first view; this just does it up front.
    """
    from catalog.images import ImageUnavailable, cache_stats, ensure_cached

    sizes = job.params.get("sizes") or ["small"]
    items = list(
        job.items.filter(status=ScrapeJobItem.Status.PENDING, card__isnull=False)
        .select_related("card", "card__piece")
    )
    # Downloads happen outside any transaction, and item bookkeeping is flushed
    # in batches: one write per image would hold SQLite's single write lock
    # thousands of times and starve the web app while the job runs.
    done = []
    failed = 0
    for index, item in enumerate(items, start=1):
        _check_cancel(job)
        piece = item.card.piece
        problems = []
        for size in sizes:
            try:
                ensure_cached(piece, size)
            except ImageUnavailable as exc:
                problems.append(str(exc))
        item.status = (
            ScrapeJobItem.Status.FAILED if len(problems) == len(sizes)
            else ScrapeJobItem.Status.DONE
        )
        item.message = "; ".join(problems)[:250]
        item.processed_at = timezone.now()
        done.append(item)
        failed += item.status == ScrapeJobItem.Status.FAILED
        if len(done) >= 50 or index == len(items):
            _flush_art_batch(job, done, failed)
            log(f"{index}/{len(items)} images cached")
            done, failed = [], 0
    stats = cache_stats()
    log(f"art cache now holds {stats['count']} images, {stats['bytes'] / 1e6:.1f} MB")


def _run_api_prices(job, log):
    """Fill prices from the catalog API instead of scraping Cardmarket.

    HONESTY: the catalog price is per CARD, not per printing — a Starlight Rare
    and a Common of the same card share one number. So it is written only to the
    trend field, tagged price_source=catalog, and by default only where no
    scraped price exists at all. The scraper stays the authority for anything
    you would actually quote a buyer.
    """
    items = list(
        job.items.filter(status=ScrapeJobItem.Status.PENDING, card__isnull=False)
        .select_related("card", "card__piece")
    )
    overwrite = job.params.get("mode") == "all"
    updated = 0
    for item in items:
        _check_cancel(job)
        card, piece = item.card, item.card.piece
        price = piece.catalog_price_eur if piece else None
        if price is None:
            item.status = ScrapeJobItem.Status.SKIPPED
            item.message = "catalog has no price for this card"
        elif (
            not overwrite
            and card.price_source == Printing.PriceSource.SCRAPE
            and card.current_price_trend
        ):
            item.status = ScrapeJobItem.Status.SKIPPED
            item.message = "keeping the scraped price"
        else:
            with transaction.atomic():
                PriceSnapshot.objects.create(
                    card=card, price_trend=price, currency="EUR",
                    observed_at=timezone.now(),
                    source=PriceSnapshot.Source.CATALOG_API, scrape_job=job,
                )
                card.current_price_trend = price
                card.price_source = Printing.PriceSource.CATALOG
                card.save(update_fields=["current_price_trend", "price_source", "updated_at"])
            updated += 1
            item.status = ScrapeJobItem.Status.DONE
            item.message = f"trend = {price} (catalog estimate)"
        item.processed_at = timezone.now()
        item.save()
        _tick(job)
    log(f"{updated} printing(s) priced from the catalog "
        f"(per-card estimates — the scraper remains the authority)")


def _flush_art_batch(job, items, failed: int) -> None:
    """Commit a batch of art results in one transaction."""
    if not items:
        return
    with transaction.atomic():
        ScrapeJobItem.objects.bulk_update(items, ["status", "message", "processed_at"])
        job.processed_items += len(items)
        job.failed_items += failed
        ScrapeJob.objects.filter(pk=job.pk).update(
            processed_items=job.processed_items,
            failed_items=job.failed_items,
            heartbeat_at=timezone.now(),
        )
