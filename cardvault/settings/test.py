from .base import *  # noqa: F403

DEBUG = False

# In-memory DB. Keep the SAME transaction regime as production
# (transaction_mode=IMMEDIATE) — collection.services relies on it for
# read-modify-write safety, because select_for_update is a no-op on SQLite.
DATABASES["default"]["NAME"] = ":memory:"  # noqa: F405
DATABASES["default"]["OPTIONS"] = {  # noqa: F405
    "init_command": "PRAGMA foreign_keys=ON;",  # WAL is meaningless for :memory:
    "transaction_mode": "IMMEDIATE",
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
