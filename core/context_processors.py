from catalog.models import Game


def games(request):
    return {"nav_games": Game.objects.all().order_by("code")}
