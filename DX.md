# Developer experience

This document explains the dev-environment changes in this PR and the reasoning behind them. The full request that triggered them is the first bullet of [the assignment](README.md#the-assignment): *"Getting this running on a fresh laptop is harder than it should be. Make it easier."*

## What we assumed

This service might be running in production somewhere. We don't know that it isn't. So the rule of thumb for every change was:

> Touch the product as little as possible. Touch the developer's machine as much as needed.

In practice that meant:

- No changes to domain code, models, the API, or migrations.
- `core/settings.py` was the only application file edited, and only to read values that were already hardcoded from environment variables instead.
- Everything new is dev-only: `Dockerfile.dev`, `docker-compose.yml`, `.dockerignore`, `.env.example`, this file, and the README section that links here.

If you `git diff` only `core/settings.py` you can confirm that: every default matches the previous hardcoded value, so behavior is identical when env vars are absent.

## What's in the box

A new dev can go from `git clone` to a running API in three commands:

```sh
cp .env.example .env
docker compose up --build
# open http://localhost:8000/api/docs
```

No `mise`, no `uv` toolchain, no local Postgres, no `createdb`. Docker is the only prereq.

### `Dockerfile.dev`

- Based on `python:3.14-slim` (same version the project's `pyproject.toml` requires).
- Installs `uv` with `pip` rather than copying a multi-stage image, to keep the file standalone and easy to read.
- Runs `uv sync --frozen --no-install-project` against the committed `uv.lock`, including dev dependencies (`pytest`, `ruff`) so tests and lint work inside the container too. The `--no-install-project` flag tells uv to only resolve third-party deps in the deps-only stage; the project itself is bind-mounted on top and `runserver` finds it via `PYTHONPATH`, so we don't need to re-bake it into the image every time a file changes.
- Installs the venv at `/venv` (outside of `/app`) and prepends it to `PATH`, so the final `CMD` is just `python manage.py runserver` — no `uv run` wrapper. Putting the venv outside the bind mount is intentional: a named volume at `/app/.venv` would mask the freshly-built venv on rebuilds, forcing `docker compose down -v` to clear stale deps.
- `runserver` is used (not `gunicorn`) so that Django's `StatReloader` picks up source edits from the bind mount.

### `docker-compose.yml`

Three services, two of which you'll actually use:

- **`db`** — `postgres:16` (the version the README requires), with a named volume for data, a port forward to `5432` so you can `psql` from the host, and a `pg_isready` healthcheck.
- **`web`** — builds from `Dockerfile.dev`, depends on `db` being healthy, bind-mounts the project at `/app` for hot reload, and runs `migrate` + `runserver` on boot.
- **`seed`** — same image, but only starts under the `seed` profile. It runs `python manage.py seed` and exits. We kept it behind a profile because seeding 600k rows takes minutes and you don't want it on every `up`.

One named volume (`web_pgcache`) keeps `uv`'s wheel cache out of the bind mount, so rebuilding the image doesn't redownload every wheel. The venv lives at `/venv` inside the image (see above), not on a volume.

### `core/settings.py`

Four lines of behavior change, all of them `os.environ.get(..., <previous literal>)`:

- `SECRET_KEY` reads `DJANGO_SECRET_KEY`.
- `DEBUG` reads `DJANGO_DEBUG` (`"1"` → `True`, anything else → `False`).
- `ALLOWED_HOSTS` reads `DJANGO_ALLOWED_HOSTS` (comma-separated).
- The `DATABASES` block reads `POSTGRES_DB`/`USER`/`PASSWORD`/`HOST`/`PORT`.

Defaults match the prior hardcoded values exactly, so the app behaves the same in production or in any environment that doesn't set these vars.

### `.env.example` and `.dockerignore`

- `.env.example` is a copy-able template. `.env` itself is git-ignored (by the user's global gitignore, not the repo's — the repo's `.gitignore` doesn't list it, which is a known minor gap we'll discuss below).
- `.dockerignore` keeps `.venv`, `.git`, `__pycache__`, the `.env`, and the docker files themselves out of the build context.

## What we deliberately didn't do

- **No production image.** This is `Dockerfile.dev` on purpose. A production image would need a non-root user, a real WSGI server (`gunicorn`/`uvicorn`), static-file collection, a separate compose file or Helm chart, and a real secret story. None of that belongs in a DX PR.
- **No seed by default.** The `seed` profile is explicit, not automatic. The first `docker compose up` is intentionally a fresh, empty DB so you can iterate against the schema without waiting minutes or dealing with 600k rows you don't need.
- **No CI changes.** The existing `pytest` setup works inside the container as-is (`docker compose exec web python -m pytest`); we didn't touch `conftest.py` or add a workflow.
- **No dependency upgrades.** The Dockerfile installs what `uv.lock` already pins. We didn't bump Django, Python, or anything else — that's a separate decision.
- **No `.env` in `.gitignore`.** It's currently caught by the user's `~/.gitignore_global`, which is enough for the people working on this today. Adding it to the repo's `.gitignore` is a one-liner if we want belt-and-suspenders, but it would commit a contract on a personal preference and we chose not to.

## What we'd do with another day

- Add a tiny `Makefile` (or `justfile`) with `up`, `down`, `logs`, `test`, `lint`, `seed`, `shell` targets. Three lines each, but they remove a lot of typing.
- Switch the dev reloader to `watchfiles` for inotify-style hot reload instead of the 1-second `StatReloader` polling.
- Add a `migrations` service that runs `makemigrations` and exits, useful for the "I edited a model, what now?" path.
- A separate `docker-compose.prod.yml` (or Helm/ECS) for the production-readiness bullet of the assignment.
- Wire `pytest` into a CI workflow.
