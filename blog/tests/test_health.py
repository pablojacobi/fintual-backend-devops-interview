"""Tests for the liveness and readiness probes at the project root."""

import warnings

import pytest
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext


@pytest.fixture
def client():
    return Client()


# --- /healthz --------------------------------------------------------------


def test_healthz_returns_200(client):
    """Liveness probe is 200 with no DB call and a fixed body."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.django_db
def test_healthz_does_not_touch_the_db(client):
    """healthz must NOT run any SQL — the orchestrator must keep
    restarting the app even when the DB is down.

    We use CaptureQueriesContext directly because the
    django_assert_num_queries fixture wraps a context that itself
    tries to open a connection, which pytest-django blocks outside
    a django_db mark. With django_db active we already have a
    connection, so the CaptureQueriesContext can use it.
    """
    with CaptureQueriesContext(connection) as ctx:
        response = client.get("/healthz")
        assert response.status_code == 200
    assert len(ctx) == 0


# --- /readyz ---------------------------------------------------------------


@pytest.mark.django_db
def test_readyz_returns_200_with_db(client):
    """Readiness probe is 200 + db=ok when the test DB is reachable."""
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "ok", "db": "ok"}


def test_readyz_returns_503_when_db_unreachable(client):
    """Readiness probe is 503 + db=down when the DB connection cannot
    serve a SELECT 1.

    The simplest way to simulate a broken DB without spinning up a
    fake one is to point POSTGRES_HOST at an unroutable address and
    make Django re-establish the connection on the next request. We
    close any cached connection first so the next cursor open has to
    reconnect, then override the host to a port nothing is listening
    on (127.0.0.1:1 is the canonical discard port).

    override_settings(DATABASES=...) emits a UserWarning at runtime;
    we silence it inside the test because we explicitly want to swap
    the host and the warning is noise.
    """
    # Drop the cached connection so the next request has to re-open it
    # with the overridden host.
    connection.close()

    with warnings.catch_warnings():
        # override_settings(DATABASES=...) emits a UserWarning at __enter__;
        # silence it before installing the override.
        warnings.simplefilter("ignore", UserWarning)
        with override_settings(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "backend_devops_interview",
                    "USER": "postgres",
                    "PASSWORD": "postgres",
                    "HOST": "127.0.0.1",
                    "PORT": "1",  # discard port: nothing is listening
                    "CONN_MAX_AGE": 0,
                },
            }
        ):
            try:
                response = client.get("/readyz")
            finally:
                connection.close()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["db"] == "down"
    # /healthz should still be 200 — liveness != readiness.
    response = client.get("/healthz")
    assert response.status_code == 200
