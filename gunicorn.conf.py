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

# --- logging ---------------------------------------------------------------

# `-` means stdout/stderr, so logs flow into `docker logs` / the
# container runtime's log collector. The level is independent of
# Django's logger level (set via DJANGO_LOG_LEVEL in settings_prod).
accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOG_LEVEL", "info")
access_log_format = '%(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sus'
