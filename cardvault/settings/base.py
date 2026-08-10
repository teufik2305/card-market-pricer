"""Shared settings for cardvault."""

from pathlib import Path

from environs import Env

env = Env()
env.read_env()

BASE_DIR = Path(__file__).resolve().parent.parent.parent
VAR_DIR = BASE_DIR / "var"
BACKUP_DIR = VAR_DIR / "backups"
SCRAPE_LOG_DIR = VAR_DIR / "scrape-logs"
# Re-hosted card art. Both catalog providers forbid hotlinking, so images are
# downloaded on first view and served from here (see catalog/images.py).
CARD_IMAGE_DIR = env.str("CARD_IMAGE_DIR", default=str(VAR_DIR / "card-images"))
# Persistent Chrome profile for the scraper. Cardmarket is behind Cloudflare;
# the clearance cookie lives here and must survive between drivers and jobs.
SCRAPE_PROFILE_DIR = env.str("SCRAPE_PROFILE_DIR", default=str(VAR_DIR / "chrome-profile"))
# How long a job waits for a Cloudflare check to clear (you clicking the
# checkbox counts) before giving up and stopping the run.
SCRAPE_CHALLENGE_TIMEOUT = env.int("SCRAPE_CHALLENGE_TIMEOUT", default=180)
# The scraper launches Chrome itself rather than letting chromedriver do it —
# chromedriver's launch switches are what Cardmarket's Cloudflare rules refuse.
SCRAPE_CHROME_BINARY = env.str(
    "SCRAPE_CHROME_BINARY",
    default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)

SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="dev-only-insecure-key-change-in-prod")
DEBUG = False
ALLOWED_HOSTS: list[str] = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django_htmx",
    "core",
    "catalog",
    "collection",
    "pricing",
    "scraping",
    "wantlists",
    "decks",
    "exports",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
]

ROOT_URLCONF = "cardvault.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.games",
            ],
        },
    },
]

WSGI_APPLICATION = "cardvault.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": VAR_DIR / "db.sqlite3",
        "OPTIONS": {
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA busy_timeout=30000;"  # a background job writes for minutes
                "PRAGMA foreign_keys=ON;"
                "PRAGMA synchronous=NORMAL;"
            ),
            "transaction_mode": "IMMEDIATE",
        },
        "ATOMIC_REQUESTS": True,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Europe/Sarajevo"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = VAR_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/accounts/login/"

# Google Sheets push (exports app). Key lives OUTSIDE git; rotate the leaked one.
GOOGLE_SHEETS_CREDENTIALS_FILE = env.str(
    "GOOGLE_SHEETS_CREDENTIALS_FILE",
    default=str(BASE_DIR / "credentials" / "card-market-pricer-44468fb06eaa.json"),
)

# Legacy JSON ledgers (read-only archive; import source).
LEGACY_DATA_DIR = env.path("LEGACY_DATA_DIR", default=str(BASE_DIR / "data"))
