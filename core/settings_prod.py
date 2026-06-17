"""Production settings.

Inherits from `core.settings` (the DX/dev defaults) and overrides the
production-only knobs. Imported by the prod `Dockerfile` via
`DJANGO_SETTINGS_MODULE=core.settings_prod`.

Fails closed on two things that the dev defaults are lenient about:
  - `DJANGO_SECRET_KEY` must be set, and must not be the dev placeholder.
  - `DJANGO_ALLOWED_HOSTS` must be set explicitly and must not contain `*`.
The dev defaults accept these (silently) because the dev path runs in a
single container with `runserver` and a permissive default host. In
prod, both of those would be a security regression; we want the
container to refuse to start instead.
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403
from .settings import BASE_DIR

# --- production toggles -----------------------------------------------------

DEBUG = False

# Fail closed on ALLOWED_HOSTS. We re-parse the env var strictly because
# the base settings accepts "*" and empty values; we want neither in prod.
_raw_hosts = os.environ.get("DJANGO_ALLOWED_HOSTS", "").strip()
if not _raw_hosts:
    raise ImproperlyConfigured(
        "DJANGO_ALLOWED_HOSTS must be set in production (comma-separated, no '*')."
    )
_hosts = [h.strip() for h in _raw_hosts.split(",") if h.strip()]
if any(h == "*" for h in _hosts):
    raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS must not contain '*' in production.")
ALLOWED_HOSTS = _hosts

# Fail closed on SECRET_KEY.
_secret = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not _secret or _secret.startswith("django-insecure-"):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a non-dev value in production. "
        "Generate one with `python -c 'from django.core.management.utils import "
        "get_random_secret_key; print(get_random_secret_key())'`."
    )
SECRET_KEY = _secret

# --- security middleware / cookies ----------------------------------------

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# --- static files ----------------------------------------------------------

# `collectstatic` in the prod Dockerfile writes into this dir. The base
# settings only declares STATIC_URL; we add the on-disk target here.
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- DB connection pool ----------------------------------------------------

# Keep a Postgres connection open per gunicorn worker for 60s. Avoids
# reconnecting on every request. psycopg_pool is a follow-up; the plain
# CONN_MAX_AGE is enough for round 1.
DATABASES["default"]["CONN_MAX_AGE"] = 60  # noqa: F405
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True  # noqa: F405

# --- logging ---------------------------------------------------------------

# JSON to stdout, level from env. python-json-logger is added in
# pyproject.toml. The base settings keeps Django's default console
# formatter for dev.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": ("%(asctime)s %(levelname)s %(name)s %(message)s"),
        },
    },
    "handlers": {
        "stdout": {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "json",
        },
    },
    "root": {
        "handlers": ["stdout"],
        "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["stdout"],
            "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO"),
            "propagate": False,
        },
        "django.db.backends": {
            "handlers": ["stdout"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
