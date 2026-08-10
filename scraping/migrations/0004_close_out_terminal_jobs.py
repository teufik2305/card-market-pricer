"""Close out items left pending by jobs that already finished.

Jobs never resumed, but the items a cancelled or failed run never reached were
left as PENDING — which made a finished job look like outstanding work waiting
to be picked up. They are marked SKIPPED so the record says what happened.
"""

from django.db import migrations

TERMINAL = ("completed", "completed_with_errors", "failed", "cancelled")


def close_out(apps, schema_editor):
    ScrapeJobItem = apps.get_model("scraping", "ScrapeJobItem")
    updated = ScrapeJobItem.objects.filter(
        status="pending", job__status__in=TERMINAL
    ).update(status="skipped", message="not attempted — the job ended first")
    if updated:
        print(f"\n  closed out {updated} item(s) stranded by finished jobs")


class Migration(migrations.Migration):
    dependencies = [("scraping", "0003_api_jobs")]
    operations = [migrations.RunPython(close_out, migrations.RunPython.noop)]
