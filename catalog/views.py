from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import FileResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.templatetags.static import static
from django.views.decorators.cache import cache_control

from collection.models import QuantityChange
from collection.services import reserved_quantity
from pricing.services import collection_totals

from . import images
from .models import CardPiece, Expansion, Game, Printing
from .normalize import normalize_name

PAGE_SIZE = 100


@cache_control(public=True, max_age=60 * 60 * 24 * 365, immutable=True)
def card_art(request, piece_id: int, size: str):
    """Serve re-hosted card art, fetching it on the first request.

    Never hotlinks: both providers blacklist for it. A miss falls back to the
    placeholder rather than 404ing, so a gallery with patchy art still renders.
    """
    piece = get_object_or_404(CardPiece, pk=piece_id)
    try:
        path = images.ensure_cached(piece, size)
    except images.ImageUnavailable:
        return HttpResponseRedirect(static("card-placeholder.svg"))
    return FileResponse(open(path, "rb"), content_type="image/jpeg")


def _money(expr):
    return ExpressionWrapper(expr, output_field=DecimalField(max_digits=14, decimal_places=2))


def game_home(request, game):
    return redirect("expansion-list", game=game)


def expansion_list(request, game):
    game = get_object_or_404(Game, code=game)
    q = request.GET.get("q", "").strip()
    owned_only = request.GET.get("owned", "1") == "1"

    expansions = game.expansions.annotate(
        total_cards=Count("printings", distinct=True),
        owned_cards=Count(
            "printings",
            filter=Q(printings__holdings__quantity__gt=0),
            distinct=True,
        ),
        owned_value_trend=Sum(
            _money(F("printings__holdings__quantity") * F("printings__current_price_trend")),
            filter=Q(printings__holdings__quantity__gt=0),
        ),
    ).order_by("slug")
    if owned_only:
        expansions = expansions.filter(owned_cards__gt=0)
    if q:
        expansions = expansions.filter(slug__icontains=q.replace(" ", "-"))

    context = {"game": game, "expansions": expansions, "q": q, "owned_only": owned_only}
    # Target check, not a bare htmx check: hx-boost makes ordinary navigation an
    # htmx request too, and boosted requests need the full page.
    if getattr(request, "htmx", False) and request.htmx.target == "expansion-rows":
        return render(request, "catalog/partials/expansion_rows.html", context)
    return render(request, "catalog/expansion_list.html", context)


def expansion_card_queryset(expansion, q: str, owned_only: bool):
    """The filtered card list of one expansion — shared by the detail page and
    the bulk-action endpoints so 'visible rows' means the same thing in both."""
    printings = (
        expansion.printings.select_related("piece")
        .annotate(qty=Sum("holdings__quantity"))
        .order_by("slug")
    )
    if q:
        printings = printings.filter(name_normalized__contains=normalize_name(q))
    if owned_only:
        printings = printings.filter(qty__gt=0)
    return printings


def expansion_panel_context(request, game, expansion, q: str, owned_only: bool) -> dict:
    printings = expansion_card_queryset(expansion, q, owned_only)
    paginator = Paginator(printings, 300)
    page = paginator.get_page(request.GET.get("page") or request.POST.get("page"))
    return {
        "game": game,
        "expansion": expansion,
        "page": page,
        "q": q,
        "owned_only": owned_only,
        "totals": collection_totals(expansion=expansion),
    }


def expansion_detail(request, game, slug):
    game = get_object_or_404(Game, code=game)
    expansion = get_object_or_404(Expansion, game=game, slug=slug)
    q = request.GET.get("q", "").strip()
    owned_only = request.GET.get("owned") == "1"

    context = expansion_panel_context(request, game, expansion, q, owned_only)
    if getattr(request, "htmx", False) and request.htmx.target == "card-panel":
        return render(request, "catalog/partials/card_panel.html", context)
    return render(request, "catalog/expansion_detail.html", context)


def expansion_totals_fragment(request, game, slug):
    game = get_object_or_404(Game, code=game)
    expansion = get_object_or_404(Expansion, game=game, slug=slug)
    return render(request, "catalog/partials/expansion_totals.html", {
        "game": game,
        "expansion": expansion,
        "totals": collection_totals(expansion=expansion),
    })


SORT_MAP = {
    "name": "slug",
    "trend": "current_price_trend",
    "-trend": "-current_price_trend",
    "qty": "-qty",
    "level": "-piece__level",
    "atk": "-piece__atk",
}
# Catalog metadata you can narrow by. Each maps a query parameter to the field
# on the linked CardPiece; the options come from what is actually in the
# database for that game, so Digimon offers colours and YuGiOh offers attributes
# without either being hard-coded.
PIECE_FACETS = {
    "type": "piece__card_type",
    "race": "piece__race",
    "attribute": "piece__attribute",
    "colour": "piece__colour",
    "archetype": "piece__archetype",
    "level": "piece__level",
}


def _facet_options(game, applied: dict) -> list[dict]:
    """Distinct values per facet, with the selection resolved here rather than
    in the template — Django templates can't index a dict by a loop variable."""
    facets = []
    for name, path in PIECE_FACETS.items():
        field = path.removeprefix("piece__")
        values = (
            CardPiece.objects.filter(game=game)
            .exclude(**{f"{field}__isnull": True})
            .values_list(field, flat=True)
            .distinct()
            .order_by(field)
        )
        values = [str(value) for value in values if value not in ("", None)]
        if not values:
            continue
        chosen = applied.get(name, "")
        facets.append({
            "name": name,
            "label": name.capitalize(),
            "options": [{"value": v, "selected": v == chosen} for v in values[:250]],
        })
    return facets


def _apply_facets(printings, request):
    applied = {}
    for name, field in PIECE_FACETS.items():
        value = request.GET.get(name, "").strip()
        if not value:
            continue
        printings = printings.filter(**{field: value})
        applied[name] = value
    return printings, applied


def card_list(request, game):
    """Collection browser: owned-only by default over the whole catalog."""
    game = get_object_or_404(Game, code=game)
    q = request.GET.get("q", "").strip()
    owned_only = request.GET.get("owned", "1") == "1"
    sort = request.GET.get("sort", "-trend")
    view = "gallery" if request.GET.get("view", "gallery") == "gallery" else "table"

    printings = (
        Printing.objects.filter(expansion__game=game)
        .select_related("expansion", "piece")
        .annotate(qty=Sum("holdings__quantity"))
    )
    if owned_only:
        printings = printings.filter(qty__gt=0)
    if q:
        printings = printings.filter(name_normalized__contains=normalize_name(q))
    printings, facets_applied = _apply_facets(printings, request)
    printings = printings.order_by(SORT_MAP.get(sort, "-current_price_trend"), "slug")

    paginator = Paginator(printings, PAGE_SIZE)
    page = paginator.get_page(request.GET.get("page"))

    context = {
        "game": game, "page": page, "q": q, "owned_only": owned_only, "sort": sort,
        "view": view, "facets": _facet_options(game, facets_applied),
        "result_count": paginator.count,
    }
    if getattr(request, "htmx", False) and request.htmx.target == "browser-panel":
        return render(request, "catalog/partials/browser_panel.html", context)
    return render(request, "catalog/card_list.html", context)


def card_detail(request, game, pk):
    game = get_object_or_404(Game, code=game)
    printing = get_object_or_404(
        Printing.objects.select_related("expansion", "piece").annotate(
            qty=Sum("holdings__quantity")
        ),
        pk=pk, expansion__game=game,
    )
    snapshots = printing.snapshots.all()[:50]
    other_printings = (
        Printing.objects.filter(
            expansion__game=game, name_normalized=printing.name_normalized
        )
        .exclude(pk=printing.pk)
        .select_related("expansion")
        .annotate(qty=Sum("holdings__quantity"))
    )
    changes = (
        QuantityChange.objects.filter(card=printing).select_related("user")[:30]
    )
    quantity = printing.qty or 0
    return render(request, "catalog/card_detail.html", {
        "game": game,
        "printing": printing,
        "piece": printing.piece,
        "reserved": reserved_quantity(printing, user=request.user),
        "line_value": (printing.current_price_trend or 0) * quantity,
        "snapshots": snapshots,
        "other_printings": other_printings,
        "changes": changes,
    })
