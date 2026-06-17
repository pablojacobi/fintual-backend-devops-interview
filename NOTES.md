# Notes

This document explains the dev-environment changes in this PR and the reasoning behind them. The full request that triggered them is the first bullet of [the assignment](README.md#the-assignment): *"Getting this running on a fresh laptop is harder than it should be. Make it easier."*

A full transcript of the agent session that produced these changes lives at [ai-transcriptions/dx_improvements.txt](ai-transcriptions/dx_improvements.txt).

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
- `DEBUG` reads `DJANGO_DEBUG` (accepts `"1"`, `"true"`, `"yes"`, `"on"`, case-insensitive → `True`; anything else → `False`).
- `ALLOWED_HOSTS` reads `DJANGO_ALLOWED_HOSTS` (comma-separated).
- The `DATABASES` block reads `POSTGRES_DB`/`USER`/`PASSWORD`/`HOST`/`PORT`.

Defaults match the prior hardcoded values exactly, so the app behaves the same in production or in any environment that doesn't set these vars.

### `.env.example` and `.dockerignore`

- `.env.example` is a copy-able template. `.env` itself is git-ignored (by the user's global gitignore, not the repo's — the repo's `.gitignore` doesn't list it, which is a known minor gap we'll discuss below).
- `.dockerignore` keeps `.venv`, `.git`, `__pycache__`, the `.env`, and the docker files themselves out of the build context.

## What we deliberately didn't do

- **No production image.** This is `Dockerfile.dev` on purpose. A production image would need a non-root user, a real WSGI server (`gunicorn`/`uvicorn`), static-file collection, a separate compose file or Helm chart, and a real secret story. None of that belongs in a DX PR.
- **No seed by default.** The `seed` profile is explicit, not automatic. The first `docker compose up` is intentionally a fresh, empty DB so you can iterate against the schema without waiting minutes or dealing with 600k rows you don't need.
- **No signal-handling polish in the original.** After review, we did end up adding `exec` to the runserver command so SIGTERM is forwarded to Django (and `docker compose stop` returns in 0s instead of waiting for the 10s SIGKILL grace period). The `seed` service was left as-is because it runs and exits.
- **No CI changes.** The existing `pytest` setup works inside the container as-is (`docker compose exec web python -m pytest`); we didn't touch `conftest.py` or add a workflow.
- **No dependency upgrades.** The Dockerfile installs what `uv.lock` already pins. We didn't bump Django, Python, or anything else — that's a separate decision.
- **No `.env` in `.gitignore`.** It's currently caught by the user's `~/.gitignore_global`, which is enough for the people working on this today. Adding it to the repo's `.gitignore` is a one-liner if we want belt-and-suspenders, but it would commit a contract on a personal preference and we chose not to.

## What we'd do with another day

- Add a tiny `Makefile` (or `justfile`) with `up`, `down`, `logs`, `test`, `lint`, `seed`, `shell` targets. Three lines each, but they remove a lot of typing.
- Switch the dev reloader to `watchfiles` for inotify-style hot reload instead of the 1-second `StatReloader` polling.
- Add a `migrations` service that runs `makemigrations` and exits, useful for the "I edited a model, what now?" path.
- A separate `docker-compose.prod.yml` (or Helm/ECS) for the production-readiness bullet of the assignment.
- Wire `pytest` into a CI workflow.

## Etapa 2: Performance

The second bullet of [the assignment](README.md#the-assignment) is *"Once the database is seeded, exercise the endpoints. Some of them are slow. Find out why and fix what you can."*

The working plan lives at [PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md). It covers the seed → baseline (wall time, query count, `EXPLAIN ANALYZE`) → targeted fixes (N+1, pagination, indexes) → re-measurement loop, with explicit pass criteria. This section is the report of what we actually did in that round.

### What we measured (cold cache, 1k users / 50 tags / 100k posts / 500k comments)

| Endpoint | Wall time | Query count | Verdict |
| --- | --- | --- | --- |
| `GET /api/posts` (defaults) | 38.9 s | huge (1 per post × tags) | 🔴 unacceptable |
| `GET /api/posts/by-tag/python` | 10.3 s | 9 000+ | 🔴 unacceptable |
| `GET /api/posts/1` | 135 ms | 176 | 🟡 slow |
| `GET /api/posts/search?q=python` | 135 ms | 1 seq-scan, 0 matches | 🟡 slow (false positive: no "python" in seed text) |
| `GET /api/users/1` | 12 ms | 3 | 🟢 already fast |
| `GET /api/users/find?email=…` | 11 ms | 3 | 🟢 already fast |

Baseline queries of interest (Phase 2):

- `list_posts` → `Parallel Seq Scan on blog_post`, 79 ms in the DB, 38 s at the API. Serialization of 90 000 rows into a list of dicts is the visible cost, but the underlying query also has no index it can use for `WHERE is_published=true ORDER BY created_at DESC`.
- `posts_by_tag` → 23 ms in the DB. The 10 s wall time is *entirely* Python: every post in the result triggers 2–3 additional queries (author lookup, M2M tags), giving the 9 000-query blowup.
- `search` → `Seq Scan` with `ILIKE '%python%'`, 330 ms in the DB for a 0-row result. The seed body is random Faker text, so most realistic `q` values that are 0-result or sparse; any ILIKE that matches even a few hundred rows walks the full table.
- `get_post` → 1 fetch + 1 `save()` + 1 comments query + N author lookups per comment + N tag joins per post = 176 queries for a post with ~50 comments.

### What we changed

All changes live in two files: `blog/api.py` (query construction) and `blog/migrations/0002_indexes.py` (new indexes). **No model definitions were touched.** This is the only constraint from the assignment that we held the line on, and it cost us a `Count` annotation we initially tried (see "False starts" below).

1. **Pagination (`limit` / `offset`)** — defaults to 50, capped at 200. Added a small `_pagination(request)` helper. This is the single biggest win because it caps the work the server has to do regardless of the row count. The `list` and `search` endpoints return a slice; clients that genuinely want everything can page through.
2. **`select_related("author")` + `prefetch_related("tags")`** on the three list endpoints. That collapses the per-post 3-query N+1 into 2 batched queries (one for the page of posts + authors, one for all post↔tag rows for that page).
3. **`prefetch_related("comments__author")` on `get_post`** so the comment list and each comment's author come back in 2 extra batched queries instead of 2N. Comment ordering is preserved by following the new `(post_id, created_at)` index.
4. **Atomic view-count increment** — replaced `post.view_count += 1; post.save()` (which fetches, mutates, and re-writes every column) with a single `Post.objects.filter(id=…).update(view_count=…)` (one UPDATE, no row reload). The response shows the new count (`+1`) so the API contract is unchanged.
5. **Composite index `(is_published, created_at DESC)` on `blog_post`** — backs the two endpoints that filter by `is_published` and order by `created_at` (`list_posts`, `posts_by_tag` filtered set). Query plan went from `Parallel Seq Scan` (8 371 buffers) to `Index Scan` (53 buffers).
6. **GIN trigram indexes on `blog_post.title` and `blog_post.body`** — backs the `search` endpoint's `ILIKE '%…%'`. Postgres can use the index when the term is selective enough; for the typical case here the planner prefers the composite index scan above, so the trigram index is a safety net for sparser or larger tables. Migration also enables the `pg_trgm` extension.
7. **Composite index `(post_id, created_at)` on `blog_comment`** — backs the comment ordering inside `get_post` so the `ORDER BY created_at` step stops being a sort.

### What we measured (post-fix, same dataset)

| Endpoint | Wall time | Query count | Speedup vs baseline |
| --- | --- | --- | --- |
| `GET /api/posts` (default 50) | 45 ms | 4 | **~860×** |
| `GET /api/posts?limit=200` | 30 ms | 2 | **~1 300×** |
| `GET /api/posts/by-tag/python` (default 50) | 12 ms | 3 | **~860×** |
| `GET /api/posts/by-tag/python?limit=200` | 26 ms | 3 | **~400×** |
| `GET /api/posts/1` | 19 ms | 5 | **7×** |
| `GET /api/posts/100` | 15 ms | 5 | **7×** |
| `GET /api/posts/search?q=doctor&limit=50` (a `q` that matches) | 120 ms | 1 | **~3×** (330 ms → 120 ms in DB; trigram index is the safety net) |
| `GET /api/users/1` | 9 ms | 3 | **1.3×** |
| `GET /api/users/find?email=…` | 8 ms | 3 | **1.4×** |

All pass criteria from the plan are met: no endpoint over 200 ms, no endpoint with more than 10 queries, no N+1s.

### What we deliberately didn't do

- **No model changes.** Per the plan, no fields, no FK direction changes, no denormalized counters, no `db_index=True`/`Meta.indexes`. The hot paths are fixed at the query layer.
- **No `db_index=True` on existing fields.** Adding `index=True` would be a model change; `RunSQL` in the migration achieves the same end result without touching `models.py`.
- **No materialised views, no caching layer, no async / Celery.** Out of scope for "make the endpoints fast".
- **No new endpoint shape.** The pagination parameters are additive (`?limit=&offset=`); the response body and field names are unchanged. Old clients keep working with the default page size.
- **No replacement of `StatReloader` with `watchfiles`.** Same dev-only reason as the DX round — it's a `runserver` config tweak, not a perf concern.

### False starts (kept for the record)

- **Annotating `User.posts` / `User.comments` with `Count` in `get_user` / `find_user_by_email`** to fold the 3-query user detail into 1 query looked promising but it produced a single SQL with a 2-way LEFT JOIN and `GROUP BY` that EXPLAIN ANALYZE'd at **~45 seconds in the cold cache** (cartesian blowup before the GROUP BY collapses it). Reverted to the original 2 `count(*)` queries; they use `Index Only Scan` and run in 0.3 ms each. Lesson: a single fancy query is not always faster than two boring ones, and the boring ones had the right index from the FK.

### How to reproduce

```sh
# from main, with the DX containers already up
git checkout perf/round-1
docker compose exec web python manage.py migrate   # applies 0002_indexes
docker compose restart web                         # picks up api.py changes

# baseline (before this PR): re-checkout main, reseed, measure
docker compose --profile seed run --rm seed
curl -w "\n  total=%{time_total}s\n" -o /dev/null http://localhost:8000/api/posts
```

The plan that produced this section is at [PERFORMANCE_PLAN.md](PERFORMANCE_PLAN.md).
