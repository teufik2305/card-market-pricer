from decimal import Decimal, InvalidOperation
from io import BytesIO

from django.http import HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone

from catalog.models import Game

from . import services

VALID_KEEPERS = {"include", "exclude", "only"}


def _params(request) -> dict:
    keepers = request.GET.get("keepers", "include")
    if keepers not in VALID_KEEPERS:
        keepers = "include"
    basis = request.GET.get("basis", "trend")
    if basis not in services.BASES:
        basis = "trend"
    min_price = None
    raw = request.GET.get("min_price", "").strip()
    if raw:
        try:
            candidate = Decimal(raw)
        except InvalidOperation:
            candidate = None
        # Decimal("nan")/Decimal("inf") parse fine but blow up in the query.
        if candidate is not None and candidate.is_finite():
            min_price = candidate
    return {"keepers": keepers, "basis": basis, "min_price": min_price}


def _filename(game, suffix: str) -> str:
    return f"{game.code}-collection-{timezone.now():%Y%m%d}.{suffix}"


def export_xlsx(request, game):
    game = get_object_or_404(Game, code=game)
    workbook = services.build_workbook(game, **_params(request))
    buffer = BytesIO()
    workbook.save(buffer)
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{_filename(game, "xlsx")}"'
    return response


def export_csv(request, game):
    game = get_object_or_404(Game, code=game)
    response = StreamingHttpResponse(
        services.csv_lines(game, **_params(request)), content_type="text/csv"
    )
    response["Content-Disposition"] = f'attachment; filename="{_filename(game, "csv")}"'
    return response
