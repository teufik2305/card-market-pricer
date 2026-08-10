"""System checks guarding assumptions the code cannot enforce locally."""

from django.conf import settings
from django.core.checks import Error, register


@register()
def sqlite_transaction_mode_check(app_configs, **kwargs):
    """collection.services does read-modify-write under select_for_update(),
    which SQLite silently ignores. Safety comes from transaction_mode=IMMEDIATE
    (writers serialize at BEGIN). Fail loudly if that config ever drifts."""
    errors = []
    db = settings.DATABASES.get("default", {})
    if "sqlite3" in db.get("ENGINE", ""):
        options = db.get("OPTIONS", {})
        if options.get("transaction_mode") != "IMMEDIATE":
            errors.append(Error(
                "SQLite must run with OPTIONS['transaction_mode'] = 'IMMEDIATE': "
                "select_for_update() is a no-op on SQLite, so IMMEDIATE transactions "
                "are the only thing preventing lost-update races in collection.services.",
                id="cardvault.E001",
            ))
    return errors
