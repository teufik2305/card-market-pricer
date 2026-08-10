"""Collection valuation — the ORM replacement for the notebook's get_worth()."""

from decimal import Decimal

from django.db.models import DecimalField, ExpressionWrapper, F, Sum

from collection.models import Holding

TOTALS_ZERO = {
    "quantity": 0,
    "total_from": Decimal("0"),
    "total_trend": Decimal("0"),
    "total_30d": Decimal("0"),
}


def _money(expr):
    return ExpressionWrapper(expr, output_field=DecimalField(max_digits=14, decimal_places=2))


def owned_queryset(game=None, expansion=None):
    qs = (
        Holding.objects.filter(quantity__gt=0)
        .select_related("card", "card__expansion", "card__expansion__game")
    )
    if game is not None:
        qs = qs.filter(card__expansion__game=game)
    if expansion is not None:
        qs = qs.filter(card__expansion=expansion)
    return qs


def collection_totals(game=None, expansion=None) -> dict:
    """Quantity + the three price-basis totals (Σ qty × current price)."""
    agg = owned_queryset(game, expansion).aggregate(
        qty_total=Sum("quantity"),
        total_from=Sum(_money(F("quantity") * F("card__current_price_from"))),
        total_trend=Sum(_money(F("quantity") * F("card__current_price_trend"))),
        total_30d=Sum(_money(F("quantity") * F("card__current_price_30d"))),
    )
    agg["quantity"] = agg.pop("qty_total")
    return {key: agg[key] if agg[key] is not None else TOTALS_ZERO[key] for key in TOTALS_ZERO}
