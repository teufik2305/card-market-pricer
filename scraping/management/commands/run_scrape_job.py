import time

from django.core.management.base import BaseCommand, CommandError

from scraping.models import ScrapeJob
from scraping.runner import run_job


class Command(BaseCommand):
    help = "Execute one scrape job (normally spawned by the scrape panel)."

    def add_arguments(self, parser):
        parser.add_argument("job_id", type=int)

    def handle(self, *args, **opts):
        # Belt and braces around the spawn-vs-commit race: wait briefly for
        # the creating transaction to land before giving up.
        for _ in range(20):
            if ScrapeJob.objects.filter(pk=opts["job_id"]).exists():
                break
            time.sleep(0.5)
        else:
            raise CommandError(f"ScrapeJob {opts['job_id']} does not exist")
        run_job(opts["job_id"])
