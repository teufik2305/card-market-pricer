from .base import *  # noqa: F403

DEBUG = False

SECRET_KEY = env.str("DJANGO_SECRET_KEY")  # noqa: F405 — required, no default in prod

# HTTPS hardening. Behind a TLS-terminating proxy (Caddy/nginx), the proxy must
# set X-Forwarded-Proto and strip it from client requests.
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = env.bool("DJANGO_SSL_REDIRECT", default=True)  # noqa: F405
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30  # raise after the setup is proven
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])  # noqa: F405
# Static files: add whitenoise (middleware + STORAGES) when deploying — see plan Phase 6.
