"""Job creation and launching.

Lifecycle: create_job (row + items, atomically) → launch_job Popens the
detached runner AFTER the request transaction commits (transaction.on_commit
in the view) → the runner itself takes the pre-job backup (BACKING_UP) and
then claims RUNNING. The DB is the only coordination medium.

Recovery: a runner process that dies without reaching a terminal status leaves
the job active. is_stalled() detects that (dead pid or stale heartbeat), the
panel shows a force-fail action, and request_cancel force-cancels stalled jobs
instead of waiting for a cooperative cancel that can never come.
"""

import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from catalog.models import Expansion, Game, Printing

from .models import ScrapeJob, ScrapeJobItem

ACTIVE_STATUSES = (
    ScrapeJob.Status.PENDING,
    ScrapeJob.Status.BACKING_UP,
    ScrapeJob.Status.RUNNING,
    ScrapeJob.Status.CANCEL_REQUESTED,
)

HEARTBEAT_STALE_AFTER = timedelta(seconds=120)
# A job that never produced a heartbeat (spawn failed, child died during
# backup) counts as stalled once it is this old.
UNCLAIMED_STALE_AFTER = timedelta(minutes=5)


class JobError(ValueError):
    pass


def active_job() -> ScrapeJob | None:
    return ScrapeJob.objects.filter(status__in=ACTIVE_STATUSES).first()


def _pid_dead(pid: int | None) -> bool:
    if not pid:
        return False  # unknown — fall back to time-based checks
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def is_stalled(job: ScrapeJob) -> bool:
    """An active job whose runner process is (very likely) dead."""
    if job.status not in ACTIVE_STATUSES:
        return False
    if _pid_dead(job.pid):
        return True
    if job.heartbeat_at is not None:
        return timezone.now() - job.heartbeat_at > HEARTBEAT_STALE_AFTER
    # Never heartbeated: give the spawn a grace period, then call it dead.
    return timezone.now() - job.created_at > UNCLAIMED_STALE_AFTER


def create_job(*, game: Game, job_type: str, params: dict, user) -> ScrapeJob:
    if active_job() is not None:
        raise JobError(
            "Another job is already active. Jobs run one at a time so they can't "
            "fight over the database or the browser."
        )

    # Savepoint: if seeding raises JobError, the half-created job row rolls
    # back too instead of wedging the launcher as a phantom pending job.
    with transaction.atomic():
        job = ScrapeJob.objects.create(
            game=game, job_type=job_type, params=params, created_by=user,
        )
        items = _seed_items(job)
        job.total_items = len(items) if items is not None else 0
        log_dir = Path(settings.SCRAPE_LOG_DIR)
        log_dir.mkdir(parents=True, exist_ok=True)
        job.log_path = str(log_dir / f"job-{job.pk}.log")
        job.save(update_fields=["total_items", "log_path", "updated_at"])
    return job


ITEM_BASED_TYPES = (
    ScrapeJob.Type.DISCOVER_CARDS,
    ScrapeJob.Type.REFRESH_PRICES,
    ScrapeJob.Type.CACHE_ART,
    ScrapeJob.Type.API_PRICES,
)


def create_retry_job(original: ScrapeJob, *, user) -> ScrapeJob:
    """Queue a new job over just the items that failed in an earlier one.

    Jobs never resume, so this is the honest version of "try those again": a
    fresh run, with its own backup and audit trail, scoped to exactly the cards
    or sets that went wrong the first time.
    """
    if original.job_type not in ITEM_BASED_TYPES:
        raise JobError("This kind of job has no per-item results to retry.")
    if not original.items.filter(status=ScrapeJobItem.Status.FAILED).exists():
        raise JobError("That job has no failed items.")
    params = dict(original.params)
    params["retry_of"] = original.pk
    return create_job(
        game=original.game, job_type=original.job_type, params=params, user=user
    )


def _seed_items(job: ScrapeJob) -> list | None:
    """Per-item checkpoints. Single-unit jobs return None."""
    retry_of = job.params.get("retry_of")
    if retry_of:
        failed = ScrapeJobItem.objects.filter(
            job_id=retry_of, status=ScrapeJobItem.Status.FAILED
        ).values_list("card_id", "expansion_id")
        items = [
            ScrapeJobItem(job=job, card_id=card_id, expansion_id=expansion_id)
            for card_id, expansion_id in failed
        ]
        if not items:
            raise JobError("That job has no failed items to retry.")
        ScrapeJobItem.objects.bulk_create(items, batch_size=2000)
        return items

    if job.job_type in (
        ScrapeJob.Type.DISCOVER_EXPANSIONS,
        ScrapeJob.Type.IMPORT_CATALOG,
        ScrapeJob.Type.RESOLVE_PRINTINGS,
    ):
        return None

    if job.job_type in (ScrapeJob.Type.CACHE_ART, ScrapeJob.Type.API_PRICES):
        cards = (
            Printing.objects.filter(expansion__game=job.game, piece__isnull=False)
            .select_related("piece")
        )
        scope = job.params.get("scope", "owned")
        if scope == "owned":
            cards = cards.filter(holdings__quantity__gt=0)
        elif scope != "all":
            raise JobError(f"Unknown scope: {scope}")

        if job.job_type == ScrapeJob.Type.API_PRICES:
            # Only cards the catalog actually has a price for.
            cards = cards.filter(piece__catalog_price_eur__isnull=False)
            if job.params.get("mode", "gaps") == "gaps":
                # A "gap" is a printing with no usable number — null OR zero.
                # Not "never scraped": the legacy import stamped
                # prices_updated_at on all 66k rows including the ones it had no
                # price for, so keying off that timestamp would match nothing.
                cards = cards.filter(
                    Q(current_price_trend__isnull=True) | Q(current_price_trend=0)
                )
        else:
            cards = cards.exclude(piece__image_small_url="", piece__image_url="")

        items = [ScrapeJobItem(job=job, card=c) for c in cards.distinct()]
        if not items:
            raise JobError(
                "Nothing to do — every card in that scope already has what this job provides."
            )
        ScrapeJobItem.objects.bulk_create(items, batch_size=2000)
        return items

    if job.job_type == ScrapeJob.Type.DISCOVER_CARDS:
        expansions = Expansion.objects.filter(game=job.game)
        slug = job.params.get("expansion")
        if slug:
            expansions = expansions.filter(slug=slug)
            if not expansions.exists():
                raise JobError(f"Unknown expansion: {slug}")
        else:
            expansions = expansions.filter(fully_scraped=False)
        items = [ScrapeJobItem(job=job, expansion=e) for e in expansions]

    elif job.job_type == ScrapeJob.Type.REFRESH_PRICES:
        cards = Printing.objects.filter(
            expansion__game=job.game, holdings__quantity__gt=0
        ).distinct()
        scope = job.params.get("scope", "stale")
        if scope == "stale":
            days = int(job.params.get("stale_days", 30))
            cutoff = timezone.now() - timedelta(days=days)
            cards = cards.filter(
                Q(prices_updated_at__lt=cutoff) | Q(prices_updated_at__isnull=True)
            )
        elif scope == "expansion":
            slug = job.params.get("expansion", "")
            if not Expansion.objects.filter(game=job.game, slug=slug).exists():
                raise JobError(f"Unknown expansion: {slug or '(none chosen)'}")
            cards = cards.filter(expansion__slug=slug)
        elif scope == "card":
            # One card, refreshed on demand — the cheapest possible scrape, for
            # when you just want a current number before quoting someone.
            card_id = job.params.get("card_id") or ""
            cards = Printing.objects.filter(pk=card_id, expansion__game=job.game) \
                if str(card_id).isdigit() else Printing.objects.none()
            if not cards.exists():
                raise JobError("Pick a card first — search for one and select it.")
        elif scope != "all":
            raise JobError(f"Unknown price-refresh scope: {scope}")
        items = [ScrapeJobItem(job=job, card=c) for c in cards]

    else:
        raise JobError(f"Unknown job type: {job.job_type}")

    if not items:
        raise JobError("Nothing to do for that scope.")
    ScrapeJobItem.objects.bulk_create(items, batch_size=2000)
    return items


def launch_job(job: ScrapeJob) -> None:
    """Spawn the detached runner. Call AFTER the creating transaction commits
    (transaction.on_commit) — the child reads its own DB connection and must
    see the job row. The pre-job backup runs in the child, so the web request
    never holds the write lock across a full-database copy."""
    manage_py = str(Path(settings.BASE_DIR) / "manage.py")
    with open(job.log_path, "a") as log_fh:
        process = subprocess.Popen(
            [sys.executable, manage_py, "run_scrape_job", str(job.pk)],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=settings.BASE_DIR,
        )
    ScrapeJob.objects.filter(pk=job.pk).update(pid=process.pid)


def _close_out_items(job: ScrapeJob, reason: str) -> None:
    """Terminal means terminal: nothing resumes, so unreached items are marked
    skipped rather than left looking like outstanding work."""
    ScrapeJobItem.objects.filter(job=job, status=ScrapeJobItem.Status.PENDING).update(
        status=ScrapeJobItem.Status.SKIPPED, message=reason[:250],
        processed_at=timezone.now(),
    )


def request_cancel(job: ScrapeJob) -> None:
    if is_stalled(job):
        # The runner is dead — a cooperative cancel would never be honored.
        _close_out_items(job, "not attempted — job cancelled while stalled")
        ScrapeJob.objects.filter(pk=job.pk, status__in=ACTIVE_STATUSES).update(
            status=ScrapeJob.Status.CANCELLED,
            error="cancelled while stalled (runner process dead)",
            finished_at=timezone.now(),
        )
    elif job.status == ScrapeJob.Status.RUNNING:
        ScrapeJob.objects.filter(pk=job.pk).update(status=ScrapeJob.Status.CANCEL_REQUESTED)
    elif job.status in (ScrapeJob.Status.PENDING, ScrapeJob.Status.BACKING_UP):
        _close_out_items(job, "not attempted — job cancelled before it started")
        ScrapeJob.objects.filter(pk=job.pk).update(
            status=ScrapeJob.Status.CANCELLED, finished_at=timezone.now()
        )


def force_fail(job: ScrapeJob, reason: str = "force-failed: runner process dead") -> None:
    """Escape hatch for stalled jobs — frees the single-job lock. Progress
    already committed (snapshots, done items) is untouched."""
    _close_out_items(job, "not attempted — job force-failed")
    ScrapeJob.objects.filter(pk=job.pk, status__in=ACTIVE_STATUSES).update(
        status=ScrapeJob.Status.FAILED,
        error=reason,
        finished_at=timezone.now(),
    )
