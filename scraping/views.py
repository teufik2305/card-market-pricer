import os
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.cache import cache
from django.db import transaction
from django.db.models import Count, F, Max, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from catalog.models import Game
from collection.models import Holding

from . import services
from .models import ScrapeJob, ScraperSettings

staff_required = user_passes_test(lambda u: u.is_staff)

TERMINAL_STATUSES = (
    ScrapeJob.Status.COMPLETED,
    ScrapeJob.Status.COMPLETED_WITH_ERRORS,
    ScrapeJob.Status.FAILED,
    ScrapeJob.Status.CANCELLED,
)


def _job_context(job: ScrapeJob) -> dict:
    eta = None
    if (
        job.status == ScrapeJob.Status.RUNNING
        and job.started_at and job.processed_items and job.total_items
    ):
        elapsed = (timezone.now() - job.started_at).total_seconds()
        per_item = elapsed / job.processed_items
        eta = int(per_item * (job.total_items - job.processed_items))
    return {
        "job": job,
        "is_terminal": job.status in TERMINAL_STATUSES,
        "is_stalled": services.is_stalled(job),
        "eta_seconds": eta,
        "progress_pct": (
            round(100 * job.processed_items / job.total_items)
            if job.total_items else None
        ),
        "log_tail": _log_tail(job),
        "skipped_items": job.items.filter(status="skipped").count(),
    }


def _log_tail(job: ScrapeJob, lines: int = 40) -> str:
    if not job.log_path or not Path(job.log_path).exists():
        return ""
    with open(job.log_path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - 8192))
        data = fh.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])


@staff_required
def panel(request):
    return render(request, "scraping/panel.html", _panel_context())


def _panel_context(**extra) -> dict:
    return {
        "active_job": services.active_job(),
        "recent_jobs": ScrapeJob.objects.select_related("game")[:20],
        "games": Game.objects.all().order_by("code"),
        # Only games that can actually import — offering the others produced a
        # job that failed the moment it started.
        "catalog_games": [g for g in Game.objects.all().order_by("code") if g.has_catalog],
        "uncatalogued": [g for g in Game.objects.all().order_by("code") if not g.has_catalog],
        "config": ScraperSettings.load(),
        "default_chrome": settings.SCRAPE_CHROME_BINARY,
        "data_health": _data_health(),
        **extra,
    }


@staff_required
@require_POST
def create_job(request):
    game = get_object_or_404(Game, code=request.POST.get("game"))
    job_type = request.POST.get("job_type", "")
    params: dict = {}
    if job_type == ScrapeJob.Type.DISCOVER_EXPANSIONS:
        params["last_n_years"] = int(request.POST.get("last_n_years", "2") or 2)
    elif job_type == ScrapeJob.Type.DISCOVER_CARDS:
        params["expansion"] = request.POST.get("expansion", "").strip()
        params["deep_search"] = request.POST.get("deep_search") == "1"
    elif job_type == ScrapeJob.Type.REFRESH_PRICES:
        params["scope"] = request.POST.get("scope", "stale")
        params["stale_days"] = int(request.POST.get("stale_days", "30") or 30)
        params["expansion"] = request.POST.get("expansion", "").strip()
        params["card_id"] = request.POST.get("card_id", "").strip()
    elif job_type == ScrapeJob.Type.IMPORT_CATALOG:
        params = {}
    elif job_type == ScrapeJob.Type.RESOLVE_PRINTINGS:
        params["redo"] = request.POST.get("redo") == "1"
        params["fuzzy"] = request.POST.get("fuzzy", "1") == "1"
    elif job_type == ScrapeJob.Type.CACHE_ART:
        params["scope"] = request.POST.get("scope", "owned")
        params["sizes"] = request.POST.getlist("sizes") or ["small"]
    elif job_type == ScrapeJob.Type.API_PRICES:
        params["scope"] = request.POST.get("scope", "owned")
        params["mode"] = request.POST.get("mode", "gaps")
    else:
        return _panel_error(request, f"Unknown job type: {job_type!r}")

    try:
        job = services.create_job(
            game=game, job_type=job_type, params=params, user=request.user
        )
    except services.JobError as exc:
        return _panel_error(request, str(exc))
    # Spawn only after this request's transaction commits — the detached child
    # reads its own connection and must see the job row.
    transaction.on_commit(lambda: services.launch_job(job))
    return redirect("scrape-job-detail", pk=job.pk)


def _panel_error(request, message: str):
    # Status 200 on purpose: under hx-boost, htmx discards 4xx bodies and the
    # error message would never be shown.
    return render(request, "scraping/panel.html", _panel_context(error=message))


@staff_required
def job_detail(request, pk):
    job = get_object_or_404(ScrapeJob.objects.select_related("game"), pk=pk)
    context = _job_context(job)
    context["failed_items"] = job.items.filter(status="failed").select_related(
        "card", "expansion"
    )[:50]
    context["can_retry"] = (
        context["is_terminal"]
        and job.failed_items
        and job.job_type in services.ITEM_BASED_TYPES
    )
    return render(request, "scraping/job_detail.html", context)


@staff_required
def job_progress(request, pk):
    """Polled fragment. Status 286 on terminal jobs tells htmx to stop polling."""
    job = get_object_or_404(ScrapeJob.objects.select_related("game"), pk=pk)
    context = _job_context(job)
    status = 286 if context["is_terminal"] else 200
    return render(request, "scraping/partials/job_progress.html", context, status=status)


@staff_required
@require_POST
def cancel_job(request, pk):
    job = get_object_or_404(ScrapeJob, pk=pk)
    services.request_cancel(job)
    job.refresh_from_db()
    return render(request, "scraping/partials/job_progress.html", _job_context(job))


@staff_required
@require_POST
def retry_failed(request, pk):
    """Start a fresh job over only what failed last time."""
    original = get_object_or_404(ScrapeJob, pk=pk)
    try:
        job = services.create_retry_job(original, user=request.user)
    except services.JobError as exc:
        return _panel_error(request, str(exc))
    transaction.on_commit(lambda: services.launch_job(job))
    return redirect("scrape-job-detail", pk=job.pk)


@staff_required
@require_POST
def force_fail_job(request, pk):
    """Escape hatch for a job whose runner process died: frees the single-job
    lock without losing any already-committed progress."""
    job = get_object_or_404(ScrapeJob, pk=pk)
    if services.is_stalled(job):
        services.force_fail(job)
    job.refresh_from_db()
    return render(request, "scraping/partials/job_progress.html", _job_context(job))


@staff_required
@require_POST
def save_settings(request):
    """Operator settings: where Chrome is, how long to wait, how politely to crawl."""
    config = ScraperSettings.load()
    chrome = request.POST.get("chrome_binary", "").strip()
    error = ""
    if chrome:
        path = Path(chrome)
        if not path.exists():
            error = f"No such file: {chrome}"
        elif path.is_dir():
            error = (f"{chrome} is a folder. On macOS point at the executable inside "
                     "the app bundle, e.g. /Applications/Google Chrome.app/Contents/"
                     "MacOS/Google Chrome")
        elif not os.access(path, os.X_OK):
            error = f"{chrome} is not executable."
    if not error:
        config.chrome_binary = chrome
        try:
            wanted = int(request.POST.get("challenge_timeout", 180))
            if wanted < MIN_CHALLENGE_WAIT:
                error = (
                    f"A Cloudflare wait of {wanted}s is shorter than the check itself: "
                    f"it normally clears on its own in 5–15 s, so anything under "
                    f"{MIN_CHALLENGE_WAIT}s abandons runs that were about to succeed. "
                    "Use 60 s or more."
                )
            config.challenge_timeout = max(MIN_CHALLENGE_WAIT, min(1800, wanted))
            config.challenge_attempts = max(0, min(10, int(request.POST.get("challenge_attempts", 4))))
            config.min_item_delay = max(0, min(600, int(request.POST.get("min_item_delay", 4))))
            config.max_item_delay = max(0, min(600, int(request.POST.get("max_item_delay", 9))))
        except (TypeError, ValueError):
            error = "Timings must be whole numbers of seconds."
    if not error:
        config.save()
        messages.success(request, "Scraper settings saved.")
        return redirect("scrape-panel")
    return render(request, "scraping/panel.html", _panel_context(error=error))


HEALTH_CACHE_SECONDS = 60
# Below this, a run gives up before the interstitial has finished clearing.
MIN_CHALLENGE_WAIT = 30


def _data_health() -> dict:
    """What each game actually has right now, so the panel answers "do I need
    to run this?" instead of leaving you to guess.

    Cached, and deliberately so: ATOMIC_REQUESTS wraps every request in a write
    transaction, and on SQLite that means these counts over 66k printings hold
    the database's single write lock for the whole render. Uncached, loading
    this page repeatedly starved a running job until it died with
    "database is locked".
    """
    cached = cache.get("scrape-data-health")
    if cached is not None:
        return cached

    from catalog.images import cache_stats
    from catalog.models import CardPiece, Printing

    from .models import ScrapeJob

    # One grouped query per fact rather than per game.
    per_game = {
        row["expansion__game"]: row
        for row in Printing.objects.values("expansion__game").annotate(
            printings=Count("id"),
            linked=Count("id", filter=Q(piece__isnull=False)),
            catalog_priced=Count("id", filter=Q(price_source=Printing.PriceSource.CATALOG)),
        )
    }
    owned = {
        row["card__expansion__game"]: row
        for row in Holding.objects.filter(quantity__gt=0).values(
            "card__expansion__game"
        ).annotate(
            owned=Count("card", distinct=True),
            # Same definition the "fill missing prices" job uses, so the
            # number on this page predicts what that job would do.
            unpriced=Count("card", distinct=True, filter=(
                Q(card__current_price_trend__isnull=True)
                | Q(card__current_price_trend=0)
            )),
        )
    }
    pieces = {
        row["game"]: row["n"]
        for row in CardPiece.objects.values("game").annotate(n=Count("id"))
    }
    imports = {
        row["game"]: row["last"]
        for row in ScrapeJob.objects.filter(
            job_type=ScrapeJob.Type.IMPORT_CATALOG, status=ScrapeJob.Status.COMPLETED,
        ).values("game").annotate(last=Max("finished_at"))
    }

    rows = []
    for game in Game.objects.all().order_by("code"):
        counts = per_game.get(game.pk, {})
        owner = owned.get(game.pk, {})
        total = counts.get("printings", 0)
        linked = counts.get("linked", 0)
        rows.append({
            "game": game,
            "pieces": pieces.get(game.pk, 0),
            "printings": total,
            "linked": linked,
            "linked_pct": round(100 * linked / total) if total else 0,
            "owned": owner.get("owned", 0),
            "owned_unpriced": owner.get("unpriced", 0),
            "catalog_priced": counts.get("catalog_priced", 0),
            "last_catalog_import": imports.get(game.pk),
        })

    stats = cache_stats()
    health = {"games": rows, "art_cached": stats["count"], "art_bytes": stats["bytes"]}
    cache.set("scrape-data-health", health, HEALTH_CACHE_SECONDS)
    return health


# -- pickers ------------------------------------------------------------------
# Typing a slug from memory is a guess; these let you search what you already
# have. Both render the same radio list, so choosing is a plain form control
# with no JavaScript and no hidden state.

PICKER_LIMIT = 20


@staff_required
def find_expansions(request):
    from catalog.models import Expansion

    game = Game.objects.filter(code=request.GET.get("game", "")).first()
    q = request.GET.get("q", "").strip()
    results = []
    if game and len(q) >= 2:
        results = list(
            Expansion.objects.filter(game=game)
            .annotate(owned=Count("printings", filter=Q(printings__holdings__quantity__gt=0),
                                  distinct=True))
            .filter(slug__icontains=q.replace(" ", "-"))
            .order_by("-owned", "slug")[:PICKER_LIMIT]
        )
    return render(request, "scraping/partials/picker_results.html", {
        "field": "expansion",
        "results": [
            {"value": e.slug, "label": e.display_name, "hint": f"{e.owned} owned"}
            for e in results
        ],
        "q": q, "too_short": 0 < len(q) < 2, "needs_game": game is None,
    })


@staff_required
def find_cards(request):
    from catalog.models import Printing
    from catalog.normalize import normalize_name

    game = Game.objects.filter(code=request.GET.get("game", "")).first()
    q = request.GET.get("q", "").strip()
    results = []
    if game and len(q) >= 2:
        results = list(
            Printing.objects.filter(
                expansion__game=game, name_normalized__contains=normalize_name(q)
            )
            .select_related("expansion")
            .annotate(owned=Sum("holdings__quantity"))
            .order_by(F("owned").desc(nulls_last=True), "slug")[:PICKER_LIMIT]
        )
    return render(request, "scraping/partials/picker_results.html", {
        "field": "card_id",
        "results": [
            {"value": c.pk, "label": c.display_name,
             "hint": f"{c.expansion.display_name} · {c.owned or 0} owned"}
            for c in results
        ],
        "q": q, "too_short": 0 < len(q) < 2, "needs_game": game is None,
    })
