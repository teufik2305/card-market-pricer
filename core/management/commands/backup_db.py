"""Safe live backup of the SQLite database (WAL-aware, via sqlite3.backup).

Retention: newest 14 'daily'/'manual' + newest 10 'pre-job' backups.
Run nightly via launchd/cron; also invoked automatically before scrape jobs.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

KEEP_REGULAR = 14
KEEP_PREJOB = 10


class Command(BaseCommand):
    help = "Back up the SQLite database to var/backups/ and prune old backups."

    def add_arguments(self, parser):
        parser.add_argument("--tag", default="manual", help="e.g. nightly, manual, pre-job-<id>")

    def handle(self, *args, **opts):
        db_path = Path(settings.DATABASES["default"]["NAME"])
        if not db_path.exists():
            raise CommandError(f"Database not found: {db_path}")
        backup_dir = Path(settings.BACKUP_DIR)
        backup_dir.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = backup_dir / f"db-{stamp}-{opts['tag']}.sqlite3"

        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        self.stdout.write(self.style.SUCCESS(f"Backup written: {target}"))

        self._prune(backup_dir)

    def _prune(self, backup_dir: Path):
        all_backups = sorted(backup_dir.glob("db-*.sqlite3"), reverse=True)
        prejob = [p for p in all_backups if "-pre-job" in p.name]
        regular = [p for p in all_backups if "-pre-job" not in p.name]
        for stale in regular[KEEP_REGULAR:] + prejob[KEEP_PREJOB:]:
            stale.unlink()
            self.stdout.write(f"Pruned old backup: {stale.name}")
