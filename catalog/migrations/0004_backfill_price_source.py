"""Label the prices that already exist.

Everything priced before this point came from Cardmarket (the legacy JSON
import or a scrape job), so it is tagged `scrape`. Without the backfill the
catalog-price job would treat real scraped prices as gaps and overwrite them
with per-card estimates.
"""

from django.db import migrations


def label_existing_as_scraped(apps, schema_editor):
    Printing = apps.get_model("catalog", "Printing")
    updated = Printing.objects.filter(
        prices_updated_at__isnull=False, price_source=""
    ).update(price_source="scrape")
    if updated:
        print(f"\n  labelled {updated} printings as Cardmarket-priced")


def clear(apps, schema_editor):
    apps.get_model("catalog", "Printing").objects.update(price_source="")


class Migration(migrations.Migration):
    dependencies = [("catalog", "0003_api_jobs")]
    operations = [migrations.RunPython(label_existing_as_scraped, clear)]
