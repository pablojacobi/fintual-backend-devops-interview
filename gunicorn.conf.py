"""Gunicorn config for the production image.

Sourced by the Dockerfile's `CMD ["gunicorn", ..., "-c", "gunicorn.conf.py"]`.
Everything that can vary per environment is read from an env var with a
sensible default; everything that is genuinely fixed is hardcoded so a
new operator doesn't have to think about it.
"""

import multiprocessing
import os

# --- bind / network --------------------------------------------------------

bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")

# --- workers ---------------------------------------------------------------
#
# Default formula `(2 * cpu_count) + 1` is the gunicorn-recommended
# starting point for sync workers on a CPU-bound workload. On a 1-vCPU
# VM that gives 3 workers fighting for 1 core — set GUNICORN_WORKERS=2
# via the deploy config in that case.
_default_workers = (2 * multiprocessing.cpu_count()) + 1
workers = int(os.environ.get("GUNICORN_WORKERS", _default_workers))

# --- timeouts --------------------------------------------------------------

# `timeout` is the seconds a worker can take to process a request before
# gunicorn kills it. 30s is the gunicorn default and matches typical
# reverse-proxy idle timeouts.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))

# `graceful_timeout` is the seconds gunicorn gives a worker to finish
# in-flight requests after receiving SIGTERM. Aligned with `timeout`.
graceful_timeout = int(os.environ.get("GUNICORN_GRACEFUL_TIMEOUT", "30"))

# `keepalive` is the seconds to wait for the next request on a kept-open
# connection. 5s matches typical reverse-proxy idle timeouts.
keepalive = int(os.environ.get("GUNICORN_KEEPALIVE", "5"))

# --- worker class ----------------------------------------------------------

# Sync workers are the right default for a Django+DRF-like workload:
# each worker process handles one request at a time, no async/event
# loop surprises. For a workload that does long blocking calls (e.g.
# external HTTP), gthread or gevent would be a better fit; that's a
# deploy-time decision, not ours.
worker_class = os.environ.get("GUNICORN_WORKER_CLASS", "sync")

# Load the WSGI app in the master process so any ImproperlyConfigured
# (bad DJANGO_SECRET_KEY, missing DJANGO_ALLOWED_HOSTS, etc.) fails
# fast and loudly at boot, before any worker has a chance to crash
# in a tight respawn loop. The cost is that the master process holds
# some memory it would otherwise have shared with workers via fork —
# for a small Django app this is negligible.
preload_app = True

# --- logging ---------------------------------------------------------------
#
# Gunicorn has its own loggers (`gunicorn.access` and `gunicorn.error`)
# that are configured separately from Django's `LOGGING` setting. To
# make every line in `docker logs` parseable as JSON, we override
# gunicorn's logging config via `logconfig_dict` and use
# `python-json-logger`'s JsonFormatter for both streams.
#
# For access logs we extract the gunicorn-arg fields (`h`, `r`, `s`,
# `b`, `D`, etc.) into top-level JSON keys. For error logs we keep
# gunicorn's standard message format but JSON-encode it.
#
# We still set `accesslog = "-"` and `errorlog = "-"` as a belt-and-
# braces: gunicorn's gLogging Logger respects the dict's handlers, so
# the `-` values are redundant when `logconfig_dict` is provided, but
# keeping them documents the intent.

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sus'


# Gunicorn's two loggers (`gunicorn.access`, `gunicorn.error`) are
# configured independently of Django's `LOGGING` setting. We use
# `logconfig_dict` to route them through a JsonFormatter so every
# line in `docker logs` parses as JSON. The `access_log_format`
# tokens above (%(h)s, %(r)s, %(s)s, %(b)s, %(D)sus, %(f)s, %(a)s)
# are the "safe atoms" listed in gunicorn.glogging.Logger atoms();
# we include them in the formatter's format string so they end up
# as top-level keys in the JSON object.
logconfig_dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": (
                "%(asctime)s %(levelname)s %(name)s %(message)s "
                "%(h)s %(r)s %(s)s %(b)s %(D)sus %(f)s %(a)s"
            ),
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
        "level": os.environ.get("GUNICORN_LOG_LEVEL", "info").upper(),
    },
    "loggers": {
        "gunicorn.error": {
            "handlers": ["stdout"],
            "level": os.environ.get("GUNICORN_LOG_LEVEL", "info").upper(),
            "propagate": False,
        },
        "gunicorn.access": {
            "handlers": ["stdout"],
            "level": os.environ.get("GUNICORN_LOG_LEVEL", "info").upper(),
            "propagate": False,
        },
    },
}
