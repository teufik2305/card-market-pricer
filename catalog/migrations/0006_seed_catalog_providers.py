"""Point the two known games at their existing providers.

Before this, the provider was a dict keyed by game code inside the importer, so
adding a game in the admin produced a traceback rather than a clear "no catalog
configured". The mapping now lives on the row.
"""

from django.db import migrations

BUILTIN = {"yugioh": "ygoprodeck", "digimon": "digimoncard"}


def seed(apps, schema_editor):
    Game = apps.get_model("catalog", "Game")
    for code, provider in BUILTIN.items():
        Game.objects.filter(code=code, catalog_provider="").update(catalog_provider=provider)


def unseed(apps, schema_editor):
    apps.get_model("catalog", "Game").objects.update(catalog_provider="")


class Migration(migrations.Migration):
    dependencies = [("catalog", "0005_catalog_provider")]
    operations = [migrations.RunPython(seed, unseed)]
