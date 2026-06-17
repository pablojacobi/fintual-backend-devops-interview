# Notes

This document is the running record of the three rounds the project went through: round 1 (dev experience), round 2 (performance), round 3 (production readiness). Each round has a transcript in [ai-transcriptions/](ai-transcriptions/) and, where a plan existed, a plan file in the repo root.

## Round 1: Dev experience

The first bullet of [the assignment](README.md#the-assignment) is *"Getting this running on a fresh laptop is harder than it should be. Make it easier."* The rule of thumb was: **touch the product as little as possible, touch the developer's machine as much as needed** — `core/settings.py` was the only application file edited, and only to read values that were already hardcoded from environment variables instead. Everything new is dev-only.

The full transcript of the agent session that produced this round is at [ai-transcriptions/dx_improvements.txt](ai-transcriptions/dx_improvements.txt).

### What's in the box

A new dev can go from `git clone` to a running API in three commands:

```sh
cp .env.example .env
docker compose up --build
# open http://localhost:8000/api/docs
```

No `mise`, no `uv` toolchain, no local Postgres, no `createdb`. Docker is the only prereq.

#### `Dockerfile.dev`

- Runs `uv sync --frozen --no-install-project` against the committed `uv.lock`, including dev dependencies (`pytest`, `ruff`) so tests and lint work inside the container too. The `--no-install-project` flag tells uv to only resolve third-party deps in the deps-only stage; the project itself is bind-mounted on top and `runserver` finds it via `PYTHONPATH`, so we don't need to re-bake it into the image every time a file changes.
- Installs the venv at `/venv` (outside of `/app`) and prepends it to `PATH`, so the final `CMD` is just `python manage.py runserver` — no `uv run` wrapper. Putting the venv outside the bind mount is intentional: a named volume at `/app/.venv` would mask the freshly-built venv on rebuilds, forcing `docker compose down -v` to clear stale deps.
- `runserver` is used (not `gunicorn`) so that Django's `StatReloader` picks up source edits from the bind mount.

#### `docker-compose.yml`

Three services, two of which you'll actually use:

- **`db`** — `postgres:16` (the version the README requires), with a named volume for data, a port forward to `5432` so you can `psql` from the host, and a `pg_isready` healthcheck.
- **`web`** — builds from `Dockerfile.dev`, depends on `db` being healthy, bind-mounts the project at `/app` for hot reload, and runs `migrate` + `runserver` on boot.
- **`seed`** — same image, but only starts under the `seed` profile. It runs `python manage.py seed` and exits. We kept it behind a profile because seeding 600k rows takes minutes and you don't want it on every `up`.

One named volume (`web_pgcache`) keeps `uv`'s wheel cache out of the bind mount, so rebuilding the image doesn't redownload every wheel. The venv lives at `/venv` inside the image (see above), not on a volume.

#### `core/settings.py`

Four lines of behavior change, all of them `os.environ.get(..., <previous literal>)`:

- `SECRET_KEY` reads `DJANGO_SECRET_KEY`.
- `DEBUG` reads `DJANGO_DEBUG` (accepts `"1"`, `"true"`, `"yes"`, `"on"`, case-insensitive → `True`; anything else → `False`).
- `ALLOWED_HOSTS` reads `DJANGO_ALLOWED_HOSTS` (comma-separated).
- The `DATABASES` block reads `POSTGRES_DB`/`USER`/`PASSWORD`/`HOST`/`PORT`.

Defaults match the prior hardcoded values exactly, so the app behaves the same in production or in any environment that doesn't set these vars.

#### `.env.example` and `.dockerignore`

- `.env.example` is a copy-able template. `.env` itself is **not** listed in this repo's `.gitignore` — it is currently caught only by the user's `~/.gitignore_global`. See the "What we deliberately didn't do" section below for why we chose not to add it.
- `.dockerignore` keeps `.venv`, `.git`, `__pycache__`, the `.env`, and the docker files themselves out of the build context.

### What we deliberately didn't do

- **No production image.** This is `Dockerfile.dev` on purpose. A production image would need a non-root user, a real WSGI server (`gunicorn`/`uvicorn`), static-file collection, a separate compose file or Helm chart, and a real secret story. None of that belongs in a DX PR. (Round 3 takes this on.)
- **No seed by default.** The `seed` profile is explicit, not automatic. The first `docker compose up` is intentionally a fresh, empty DB so you can iterate against the schema without waiting minutes or dealing with 600k rows you don't need.
- **No signal-handling polish in the original.** After review, we did end up adding `exec` to the runserver command so SIGTERM is forwarded to Django (and `docker compose stop` returns in 0s instead of waiting for the 10s SIGKILL grace period). The `seed` service was left as-is because it runs and exits.
- **No `.env` in `.gitignore`.** It's currently caught by the user's `~/.gitignore_global`, which is enough for the people working on this today. Adding it to the repo's `.gitignore` is a one-liner if we want belt-and-suspenders, but it would commit a contract on a personal preference and we chose not to.

### What we'd do with another day

- A tiny `Makefile` (or `justfile`) with `up`, `down`, `logs`, `test`, `lint`, `seed`, `shell` targets. Three lines each, but they remove a lot of typing.
- A `migrations` service that runs `makemigrations` and exits, useful for the "I edited a model, what now?" path.

## Round 2: Performance

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

1. **Pagination (`limit` / `offset`)** — defaults to 50, capped at 200. Declared in the Django Ninja function signature (e.g. `def list_posts(request, limit: int = 50, offset: int = 0)`), so the params show up in `/api/docs` and invalid input returns a proper 422 from Ninja's validator. We clamp the values in the handler (a small `_clamp` helper) so an attacker can't pass `limit=999999` and re-introduce the original problem. This is the single biggest win because it caps the work the server has to do regardless of the row count. The `list` and `search` endpoints return a slice; clients that genuinely want everything can page through.
2. **`select_related("author")` + `prefetch_related("tags")`** on the three list endpoints. That collapses the per-post 3-query N+1 into 2 batched queries (one for the page of posts + authors, one for all post↔tag rows for that page).
3. **`Prefetch("comments", queryset=Comment.objects.select_related("author").order_by("created_at"))` on `get_post`** so the comment list and each comment's author come back in 2 extra batched queries instead of 2N, with explicit `ORDER BY` (databases don't guarantee ordering from an index alone). The new `(post_id, created_at)` index backs that sort.
4. **Atomic view-count increment** — replaced `post.view_count += 1; post.save()` (which fetches, mutates, and re-writes every column) with a single `Post.objects.filter(id=…).update(view_count=F("view_count") + 1)` (one UPDATE, no row reload, race-condition-free at the DB level). The response shows the new count (`+1`) so the API contract is unchanged.
5. **Partial index `(created_at DESC) WHERE is_published = true` on `blog_post`** — backs the two endpoints that filter by `is_published` and order by `created_at` (`list_posts`, `posts_by_tag` filtered set). The `WHERE is_published = true` predicate keeps the index small (it skips drafts), reduces write amplification, and is still a perfect match for both query plans. Query plan went from `Parallel Seq Scan` (8 371 buffers) to `Index Scan` (53 buffers).
6. **Partial GIN trigram indexes on `blog_post.title` and `blog_post.body`, both with `WHERE is_published = true`** — backs the `search` endpoint's `ILIKE '%…%'`. Postgres can use the indexes when the term is selective enough; for the typical case here the planner prefers the composite index scan above, so the trigram indexes are a safety net for sparser or larger tables. The partial predicate is the same `is_published = true` used in the read paths, so the GIN — which is large and expensive to maintain, especially on `body` — only covers the rows we ever query. Migration also enables the `pg_trgm` extension.
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
| `GET /api/posts/search?q=doctor&limit=50` (a `q` that matches) | 120 ms | 1 | **~2.7×** (330 ms → 120 ms in DB; trigram index is the safety net) |

All pass criteria from the plan are met: no endpoint over 200 ms, no endpoint with more than 10 queries, no N+1s. (The `users` endpoints were already fast in the baseline — 12 ms / 11 ms — and stayed fast; we did not include them in the post-fix table because the speedup is cosmetic.)

### What we deliberately didn't do

- **No model changes.** Per the plan, no fields, no FK direction changes, no denormalized counters, no `db_index=True`/`Meta.indexes`. The hot paths are fixed at the query layer.
- **No `db_index=True` on existing fields.** Adding `index=True` would be a model change; `RunSQL` in the migration achieves the same end result without touching `models.py`.
- **No materialised views, no caching layer, no async / Celery.** Out of scope for "make the endpoints fast".
- **No new endpoint shape.** The pagination parameters are additive (`?limit=&offset=`); the response body and field names are unchanged. Old clients keep working with the default page size.
- **No replacement of `StatReloader` with `watchfiles`.** Same dev-only reason as the DX round — it's a `runserver` config tweak, not a perf concern.

### Candidates we considered and explicitly dropped

After the round landed, we looked at two more candidates for a fast follow-up. We measured the baselines and decided not to ship either. The reasoning is here so the next person doesn't have to redo the analysis.

- **`only()` / `defer()` on the list endpoints to stop the ORM from materialising unused columns.** `PostListOut` exposes `id, title, author, tags, view_count, created_at`; `blog_post` also has `body` (~550 bytes/row on this dataset) and `updated_at`, neither of which the list serializer touches. With 50 rows that's ~27 KB of data the ORM is fetching and immediately discarding. We measured the cold-cache cost: `EXPLAIN (ANALYZE, BUFFERS)` shows the query plan already loads only 86 bytes/row from the heap (Postgres TOAST is lazy for `body`), and the wall time of `/api/posts` is 9 ms cold vs 0.1 ms in the DB. The Python overhead of building the queryset with `only()` and the maintenance cost of keeping the field list in sync with `PostListOut` exceed the savings at the current scale. **Decision: not worth the change.** The break-even is around datasets where `body` is large and not TOAST-eligible (e.g. several KB text fields on the same row), or list pages much larger than 50.

- **`tsvector` column + GIN index for `search_posts`.** This is the only path to a real `search` improvement: the query plan today still walks `blog_post_pub_created_idx` and applies `ILIKE '%x%' OR ILIKE '%x%'` as a filter — 3.4 ms in the DB but ~180 ms of wall time in the ORM/serializer, and 0.5–1.0 s wall time on colder caches. A `tsvector` column maintained by a trigger, with a GIN index, replaces that with a single index lookup. Expected wall time: ~30–50 ms (10× drop). **Decision: not in this round.** The change requires a new field on `blog_post` (model-definition change), a trigger or `BeforeInsert` hook, a backfill of the 100k seeded rows, and a re-seed or `UPDATE … LIMIT 10000` loop to avoid long-held locks. None of that is unsafe individually, but combined it is the kind of change that needs test coverage on the trigger, on the backfill's idempotency, and on the seed → search round-trip. The test suite today covers the API but not the schema. We are not comfortable shipping the change without that test scaffolding; adding the tests first is itself a half-day of work that doesn't show up as a perf improvement. **Recommended next step:** add `pytest-django` cases for the trigger and the backfill, then ship `tsvector` as round 2.

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

## Round 3: Production readiness

The third bullet of [the assignment](README.md#the-assignment) is *"This service is a long way from something you'd put behind a load balancer. Move it closer."* The full working plan is at [PROD_PLAN.md](PROD_PLAN.md); this section is the report of what shipped.

### Rule of thumb

> Make the artifact deployable and document the recipe; do not actually deploy it.

In practice that meant:

- **No real infra, no CD, no secrets manager.** A real deployment would involve at least Terraform/Pulumi for the VM, a secrets store (Vault, AWS Secrets Manager, SOPS), a CD pipeline (GitHub Actions + ArgoCD / Spinnaker / equivalent), and a log/metric pipeline. None of that is in this round. What is in this round is the image, the gunicorn config, the prod settings, the health probes, the Caddyfile, and a `docker-compose.prod.yml` that brings all four services up on a single laptop behind a self-signed cert.
- **Production settings live in their own file.** `core/settings_prod.py` inherits from `core/settings.py` and overrides only the prod-only knobs. Dev still uses `core.settings` everywhere (Dockerfile.dev, docker-compose, pytest, the existing test fixtures). The split is real, not a one-line flag: it makes accidental "run prod settings in dev" harder to do, and it makes the prod-only invariants (`ALLOWED_HOSTS` no `*`, `SECRET_KEY` not the dev placeholder) fail-closed at import time.
- **The prod image is the only artifact.** No "deploy from the dev image, just override the command". The Dockerfile is multi-stage, non-root, has `collectstatic` baked in, and runs gunicorn as PID 1 so SIGTERM reaches it directly. Bind mounts are gone.
- **Local prod-like is the verification surface.** A `docker-compose.prod.yml` stack with a `prod` profile brings the image up behind Caddy and lets us curl the real wire. If the artifact doesn't boot there, it doesn't ship.

### What we changed

All changes are net-additive to the dev path; nothing in `Dockerfile.dev`, `docker-compose.yml`, or `core/settings.py` was broken by this round.

1. **Lint cleanup (`pyproject.toml`, `blog/admin.py`, `blog/management/commands/seed.py`).** Prereq for the CI step. `ruff check .` was reporting 9 issues — 1 unused import, 1 unused loop variable, 7 long lines in `blog/migrations/0001_initial.py` (auto-generated, so we now skip `blog/migrations/*` in ruff's `extend-exclude`). Also ran `ruff format` on the 6 files that hadn't been formatted yet; mechanical, no semantic change.

2. **Settings split (`core/settings_prod.py`).** New module that imports from `core.settings` and overrides the prod knobs. Three fail-closed guards at import time:
   - `DJANGO_ALLOWED_HOSTS` must be set and must not contain `*` (the dev default of `*` would be a security regression in prod).
   - `DJANGO_SECRET_KEY` must be set and must not match the dev placeholder (`django-insecure-*`).
   - `STATIC_ROOT` is defined in the base settings (it has to be: the prod `Dockerfile` runs `collectstatic` during build, against the dev settings module, to bake the static files into the image).
   Other prod knobs: `SECURE_PROXY_SSL_HEADER`, `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, HSTS for 1 year with includeSubDomains + preload, `SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_REFERRER_POLICY='same-origin'`, `X_FRAME_OPTIONS='DENY'`, `CONN_MAX_AGE=60`, `CONN_HEALTH_CHECKS=True`, and a JSON `LOGGING` block with python-json-logger.

3. **Production `Dockerfile` (multi-stage, non-root, `collectstatic` baked).**
   - `builder` stage: `python:3.14-slim`, `uv sync --frozen --no-install-project --no-dev`, then `python manage.py collectstatic --noinput` against the dev settings module. The static files are baked into the runtime stage.
   - `runtime` stage: same base, copies `/venv` and `/app` from the builder, creates an `app` user (uid 1000), chowns `/app`, `USER app`, `EXPOSE 8000`, `CMD ["gunicorn", "core.wsgi:application", "-c", "gunicorn.conf.py"]`. `gunicorn` is PID 1 — SIGTERM from `docker stop` reaches it directly, the 30s `graceful_timeout` gives workers headroom to finish in-flight requests.

4. **`gunicorn.conf.py`.** Read-only config that the operator can override via env vars: `bind=0.0.0.0:8000`, `workers=(2*cpu_count)+1` (gunicorn's recommended default for sync workers), `worker_class=sync`, `timeout/graceful_timeout/keepalive = 30/30/5s`, `accesslog=errorlog="-"` (stdout). `preload_app=True` so any `ImproperlyConfigured` from `core/settings_prod.py` fails fast at the master's boot, not in a worker respawn loop. `logconfig_dict` routes gunicorn's own `gunicorn.access` and `gunicorn.error` loggers through a `pythonjsonlogger.jsonlogger.JsonFormatter`, so every line in `docker logs` parses as JSON (gunicorn's default formatter would otherwise emit plain text and break the rule).

5. **Health endpoints (`core/health.py` + `core/urls.py` + `blog/tests/test_health.py`).** Two endpoints at the project root, NOT under `/api/`, so an orchestrator can hit them without coupling to the app routing:
   - `GET /healthz` — liveness. Returns 200 + `{"status":"ok"}` with **zero DB queries** (a `CaptureQueriesContext` test asserts this). The orchestrator must keep this instance alive even when the DB is down.
   - `GET /readyz` — readiness. Runs `SELECT 1` against the default connection; returns 200 + `{"status":"ok","db":"ok"}` on success, 503 + `{"status":"degraded","db":"down","error":"…"}` on any failure. The probe has a 1-second connect timeout so a hung DB doesn't block the readiness gate. Tests cover both 200 and 503 paths.

6. **`docker-compose.prod.yml` + `Caddyfile` + `.env.prod.example` + `.gitignore`.** A `prod` profile brings up `db` (no host port), `migrate` (one-shot, `restart: "no"`, web waits via `service_completed_successfully`), `web` (no host port, only Caddy can reach it), and `caddy` (caddy:2, ports 80+443, mounts the Caddyfile). The Caddyfile uses `tls internal` (self-signed cert) for local prod-like, with a commented-out `on_demand` server block showing what changes for a real deploy. `transport http { versions 1.1 }` pins the upstream to HTTP/1.1 (gunicorn doesn't speak h2). `.env.prod.example` documents every env var with a redacted fake value. `.gitignore` gains `.env.prod` and `staticfiles/`.

7. **GitHub Actions CI (`.github/workflows/ci.yml`).** Two jobs, both on `pull_request` and `push` to `main`. **`test`** boots a `postgres:16` service, runs `uv sync --frozen` against the committed `uv.lock` (which already pins pytest, pytest-django, ruff and every runtime dep), then `uv run python -m django migrate --noinput` and `uv run pytest -q`. **`lint`** does the same `uv sync --frozen`, then `uv run ruff check .` and `uv run ruff format --check .`. No deploy step, no artifact publish, no matrix. One Python version (3.14), one Postgres version (16).

### What we deliberately didn't do

- **No Helm chart, K8s manifests, ECS task def, Fly, or Render config.** The recipe in `PROD_PLAN.md` is "plain Docker + Caddy on a single VM". The Dockerfile + compose + Caddyfile are the minimum that gets you to "one command away from a deploy"; converting that to a K8s manifest is a follow-up and a one-day job. We did not pre-build it because (a) the deployment target depends on where the company actually runs things, and (b) building it well requires the same Terraform/Vault work that's out of scope this round.
- **No actual deployment.** Per the rule of thumb. The compose stack comes up and serves the API over `https://localhost` with a self-signed cert. We did not point a real domain at it.
- **No secrets manager integration.** `DJANGO_SECRET_KEY` and `POSTGRES_PASSWORD` live in `.env.prod`, which is in `.gitignore`. A real deployment would have these injected from Vault / SOPS / the orchestrator's secret store. The fail-closed guards in `core/settings_prod.py` are the contract: if the env var is missing or matches the dev placeholder, the container refuses to start. Whichever secret store you wire in, the contract is the same.
- **No CD.** CI runs tests and lint on PR and push-to-main. There is no `deploy` job, no GHCR push, no `kubectl apply`. Adding a CD step is a separate round and depends on the deployment target.
- **No observability stack.** Logs are JSON to stdout (gunicorn + Django). There is no Prometheus exporter, no OpenTelemetry tracing, no Datadog agent. The JSON logs are structured enough for any log collector to parse; that's the part of "production observability" we picked up.
- **No rate limiting, no WAF, no auth proxy.** All out of scope per the assignment ("authentication / authorization is intentionally absent").
- **No `Dockerfile.dev` changes.** It still does `uv sync --frozen --no-install-project` against the committed `uv.lock` and runs `runserver`. The lint cleanup touched `pyproject.toml`'s `extend-exclude`, which `ruff` reads inside the dev container too; the dev path is otherwise untouched.

### Risks and what we'd do with another day

- **The test suite covers health probes but not the actual API surface.** `blog/tests/test_health.py` exercises `/healthz` and `/readyz`, and `blog/tests/test_posts.py` is a smoke test of the `Post` model. None of the real read endpoints — `list_posts`, `get_post`, `posts_by_tag`, `search`, `list_users`, `get_user`, `find_user_by_email` — have a contract test. We relied on the seed + manual `curl` loop (and the perf-round timings) to catch regressions, and the perf round did surface one (the `Count`-annotation cartesian blowup) by re-measuring wall time, not via a failing test. That is fine while one person knows the codebase; it is a liability the moment a second person edits the same code or we revisit the `tsvector` candidate from Round 2, which we explicitly deferred because there were no tests to land it safely behind. **Recommended next step:** add a `pytest-django` case per endpoint that pins the response shape, the pagination contract, the `is_published` filter, and at least one error path (404 on missing post, 422 on bad `limit`/`offset`). The Django Ninja test client makes this cheap; one file per endpoint group, ~10 cases total, no new fixtures beyond the existing seed.
- **Single-VM Caddy as the public entrypoint is a single point of failure.** The image is fine, but the deployment shape (one VM, one Caddy, one DB volume) isn't HA. Two more VMs behind a load balancer, Postgres to RDS, Caddy to a managed TLS terminator — that's the "move to a managed platform" round.

### How to reproduce

```sh
# from a clean checkout of chore/prod-readiness
cp .env.prod.example .env.prod
# edit DJANGO_SECRET_KEY; you can use:
#   python3 -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
docker compose -f docker-compose.prod.yml --profile prod \
    --env-file .env.prod up --build
# wait for the four services to come up
sleep 3
curl -sk https://localhost/healthz      # {"status":"ok"}            <10ms
curl -sk https://localhost/readyz       # {"status":"ok","db":"ok"}  ~10ms
curl -sk https://localhost/api/posts    # the seeded JSON list (paginated)
docker compose -f docker-compose.prod.yml --profile prod \
    --env-file .env.prod down -v        # tear down + remove named volumes
```

The plan that produced this section is at [PROD_PLAN.md](PROD_PLAN.md). The full transcript of the agent session that produced this round is at [ai-transcriptions/prod_readiness_round.txt](ai-transcriptions/prod_readiness_round.txt).
