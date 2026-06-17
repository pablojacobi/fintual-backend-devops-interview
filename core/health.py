"""Liveness and readiness endpoints.

Two endpoints, both at the project root (NOT under /api/):

- `/healthz` is the liveness probe. It returns 200 as long as the
  Python process can answer a request. It deliberately does NOT touch
  the database: an unhealthy DB should not cause Kubernetes / a load
  balancer to restart the app, only to mark it not-ready (below).

- `/readyz` is the readiness probe. It returns 200 only when the
  default database connection can serve a `SELECT 1` within a 1-second
  timeout. It returns 503 otherwise. The reverse proxy and the
  orchestrator should use this to decide whether to send traffic to
  this instance.

Why a custom 1-second timeout? The default psycopg connect timeout is
10 seconds, which is way too long for a health check that the
orchestrator is going to call every few seconds. 1s is long enough to
absorb a normal cold connect and short enough that a hung DB doesn't
block the readiness gate for an entire healthcheck interval.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import connections
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

# Cap the readyz check at 1s. The default psycopg connect timeout is
# 10s; we want health checks to fail fast so the orchestrator can
# decide quickly what to do.
_READYZ_TIMEOUT_S = 1.0


def _readyz_db_check() -> tuple[bool, dict[str, Any]]:
    """Return (ok, payload) for the DB probe.

    Tries a `SELECT 1` with a 1s timeout. On any failure, logs a
    warning and reports db=down. We do not raise; the caller turns
    this into a 503 response.
    """
    conn = connections["default"]
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True, {"db": "ok"}
    except Exception as exc:  # noqa: BLE001 - health check is exactly the place to catch all
        logger.warning("readyz: DB probe failed: %s", exc)
        return False, {"db": "down", "error": str(exc)}


@require_GET
def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness probe: 200 if the process can serve a request."""
    return JsonResponse({"status": "ok"}, status=200)


@require_GET
def readyz(request: HttpRequest) -> JsonResponse:
    """Readiness probe: 200 + db=ok if the DB answers SELECT 1, else 503."""
    # Hint psycopg/psycopg2 to use a short connect timeout. We mutate
    # the live connection's settings rather than the dict in
    # settings.DATABASES because that's the only way to take effect
    # for an already-open connection.
    conn = connections["default"]
    try:
        if hasattr(conn, "get_connection_params"):
            params = conn.get_connection_params()
            # psycopg and psycopg2 both honour `connect_timeout` in seconds.
            params["connect_timeout"] = _READYZ_TIMEOUT_S
    except Exception:  # noqa: BLE001
        # If we can't read the params (very old driver, weird backend),
        # fall through and let the cursor call do its own thing.
        pass

    ok, payload = _readyz_db_check()
    status = 200 if ok else 503
    body = {"status": "ok" if ok else "degraded"}
    body.update(payload)
    return JsonResponse(body, status=status)
