# Performance round 1 — plan

## Goal

Take the seeded DB and make every endpoint as fast as it can reasonably get **without changing the model definitions** (`Post`, `User`, `Tag`, `Comment` stay as they are in [`blog/models.py`](blog/models.py)). Pagination, ordering and serialization tweaks are fair game; schema changes are not.

Decision rule, written to [`NOTES.md`](NOTES.md) at the end:

> We did not add fields, change FK direction, or denormalize the schema. We did add indexes, fix N+1s, and add a page/limit contract to list endpoints. Pagination was discussed and approved before being added.

---

## Phase 0 — diagnostic setup (no app code change)

1. Confirm `web-1` and `db-1` are still up from the last DX round. If not, `docker compose up --build`.
2. Open a `psql` shell into the DB container for `EXPLAIN ANALYZE` work:
   ```sh
   docker compose exec db psql -U postgres -d backend_devops_interview
   ```
3. Keep Django's built-in query log ready by exporting `DJANGO_DEBUG=1` (already the default in `.env`). We will use `connection.queries` from `python manage.py shell` to count queries per endpoint.

No new dev dependencies. No `INSTALLED_APPS` changes. No middleware added.

---

## Phase 1 — seed the database

Run the seed in the foreground and capture the elapsed time, because we'll quote it in `NOTES.md`:

```sh
time docker compose --profile seed run --rm seed
```

Expected: 1–5 min depending on the Mac. Do not bail out early. After it finishes, sanity-check the row counts from `psql`:

```sql
SELECT
  (SELECT count(*) FROM blog_user)    AS users,
  (SELECT count(*) FROM blog_tag)     AS tags,
  (SELECT count(*) FROM blog_post)    AS posts,
  (SELECT count(*) FROM blog_comment) AS comments;
```

Expected roughly: 1k / 50 / 100k / 500k. If any number is off, we debug before moving on.

---

## Phase 2 — baseline: measure every endpoint, no fixes

For each of the 6 list/detail endpoints, capture three numbers:

1. **Wall time** from the API caller's perspective:
   ```sh
   for ep in \
     '/api/posts' \
     '/api/posts?limit=50' \
     '/api/posts/search?q=python' \
     '/api/posts/by-tag/python' \
     '/api/posts/by-tag/python?limit=50' \
     '/api/posts/1' \
     '/api/users/1' \
     '/api/users/find?email=user00001@example.com'; do
     printf '%-55s ' "$ep"
     curl -s -o /dev/null -w 'HTTP %{http_code}  total=%{time_total}s\n' "http://localhost:8000$ep"
   done
   ```
   (`limit` doesn't exist yet — we add it later — so the 2nd and 5th lines are aspirational; we capture the current "no limit" wall time and the "with limit" wall time after the fix.)
2. **Query count** per endpoint, via `connection.queries` from `manage.py shell`. Helper:
   ```python
   from django.db import connection, reset_queries
   from django.test import Client
   c = Client()
   reset_queries(); c.get("/api/posts"); print(len(connection.queries), [q["sql"][:60] for q in connection.queries[:3]])
   ```
   Repeat per endpoint. Record both the count and the shape of the first 3 SQL strings.
3. **`EXPLAIN (ANALYZE, BUFFERS)`** for the heavy queries we identify. Likely candidates up front:
   - `SELECT … FROM blog_post WHERE is_published ORDER BY created_at DESC`
   - `SELECT … FROM blog_post WHERE title ILIKE %x% OR body ILIKE %x% AND is_published ORDER BY created_at DESC`
   - `SELECT … FROM blog_post_tags WHERE tag_id IN (…)` (M2M reverse)
   - `SELECT … FROM blog_comment WHERE post_id = $1 ORDER BY created_at`
   - `SELECT count(*) FROM blog_post WHERE author_id = $1`

Write all of this to `NOTES.md` under a "Baseline" section. This is the "are we done yet?" answer key for the rest of the round.

---

## Phase 3 — fixes (the order matters)

Apply one fix, re-measure, write the delta to `NOTES.md`, then move to the next. Do not batch them.

### Fix 1: N+1s in [`blog/api.py`](blog/api.py)

`_serialize_post_list` accesses `post.author` and `post.tags.all()` per row. With 50 posts in a list, that's `1 + 50 + 50 = 101` queries. Two of them are the same `SELECT … FROM blog_user WHERE id IN (?, ?, ?)` repeated 50 times, and same for tags.

In [`blog/api.py`](blog/api.py), change the three list endpoints (`list_posts`, `search_posts`, `posts_by_tag`) to:

```python
qs = (
    Post.objects
    .select_related("author")
    .prefetch_related("tags")
    .filter(...)
    .order_by("-created_at")
)
```

Same for `get_post` (`post.author` + `post.tags` + `post.comments` access). And for `_user_detail` (`user.posts.count()` + `user.comments.count()` are 2 queries; replacing with annotations makes it 0).

### Fix 2: pagination

The current endpoints return **all** published posts (or all matching a search) with no cap. With 100k posts, even after the N+1 fix, the response is huge and the JSON serializer dominates wall time. Add a hard cap and a `limit` query param.

In [`blog/api.py`](blog/api.py):

```python
DEFAULT_LIMIT = 50
MAX_LIMIT = 200

def _paginate(request, default=DEFAULT_LIMIT, max_=MAX_LIMIT) -> tuple[int, int]:
    try:
        limit = int(request.GET.get("limit", default))
    except ValueError:
        limit = default
    limit = max(1, min(limit, max_))
    try:
        offset = int(request.GET.get("offset", 0))
    except ValueError:
        offset = 0
    offset = max(0, offset)
    return limit, offset
```

Then in each list endpoint: `qs = qs[offset:offset + limit]`. Document in `NOTES.md` that pagination was added; previously the endpoint was unbounded and would have OOM'd in any non-toy deployment.

### Fix 3: indexes

Based on the `EXPLAIN ANALYZE` from Phase 2, write a new migration `blog/migrations/0002_indexes.py` (not `models.py` — no schema change to the model definitions, just SQL):

- `blog_post`: composite `(is_published, created_at DESC)` — supports the `WHERE is_published=true ORDER BY created_at DESC LIMIT 50` pattern. Postgres can scan this index backwards and stop at 50, so the cost is O(limit), not O(rows).
- `blog_post`: GIN trigram on `(title, body)` via `pg_trgm` extension — supports `ILIKE` without a full table scan. Required because Phase 2 will show `icontains` is a seq scan.
- `blog_post`: `(author_id)` — already covered by the FK index Django creates, but verify in `EXPLAIN`.
- `blog_post_tags` (M2M through): composite `(tag_id, post_id)` — supports the reverse join used by `posts_by_tag`. The default `(post_id, tag_id)` PK index is the wrong direction.
- `blog_comment`: `(post_id, created_at)` — supports `get_post`'s `comments.order_by("created_at")`.
- `blog_post`: partial index on `view_count` only if any endpoint sorts by it; current API doesn't, so **skip**.

Migration content sketch:

```python
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [("blog", "0001_initial")]
    operations = [
        CreateExtension("pg_trgm"),
        migrations.RunSQL(
            "CREATE INDEX blog_post_pub_created_idx ON blog_post (is_published, created_at DESC)"
        ),
        migrations.RunSQL(
            "CREATE INDEX blog_post_title_trgm ON blog_post USING gin (title gin_trgm_ops)"
        ),
        migrations.RunSQL(
            "CREATE INDEX blog_post_body_trgm  ON blog_post USING gin (body  gin_trgm_ops)"
        ),
        migrations.RunSQL(
            "CREATE INDEX blog_post_tags_tag_post_idx ON blog_post_tags (tag_id, post_id)"
        ),
        migrations.RunSQL(
            "CREATE INDEX blog_comment_post_created_idx ON blog_comment (post_id, created_at)"
        ),
    ]
```

The `pg_trgm` extension is created at the role level. `createdb` from the docker image already has it, but `CreateExtension` is idempotent and makes the migration self-contained.

### Fix 4: `search_posts` is allowed to be slow

`ILIKE %x%` with a trigram index is still not free. If Phase 2 shows `search` is in the multi-second range, the honest call is:

- Document it in `NOTES.md` under "What we deliberately didn't do".
- Note that a proper fix needs `tsvector` + GIN or a dedicated search engine (Meilisearch, ES), which is out of scope for "no model changes".

We do **not** add a search backend in this round.

### Fix 5: `view_count += 1` write amplification

Every `GET /api/posts/{id}` does a `UPDATE blog_post SET view_count = view_count + 1`. At any kind of traffic this destroys write throughput and creates row-lock contention.

If Phase 2 shows this is in the hot path, replace with a fire-and-forget increment via `Post.objects.filter(id=post_id).update(view_count=F("view_count") + 1)`, or skip the increment entirely. Document the choice in `NOTES.md`.

---

## Phase 4 — re-measure

Run the same Phase 2 measurement script. For each endpoint, record the new wall time, new query count, and a re-run of `EXPLAIN (ANALYZE, BUFFERS)` on the queries that changed plan.

Pass criteria (the "are we done?"):

- Every list endpoint returns in **under 100 ms p50** for `limit=50` on the seeded DB.
- Every list endpoint issues **at most 4 queries** total (1 for the page, 1 prefetch for tags, 1 prefetch for comments if any, 1 count if any).
- `EXPLAIN ANALYZE` on the heavy list query shows an Index Scan (or Index Only Scan) using `blog_post_pub_created_idx`, never a Seq Scan.
- `search_posts` either uses the GIN trigram index (under, say, 500 ms) or is explicitly documented as out-of-scope.

If any criterion fails, return to Phase 3 with the failing endpoint. Do not declare done.

---

## Phase 5 — write up

Append a new section to [`NOTES.md`](NOTES.md) titled "Performance round 1". Sections:

1. **What we measured** — table of endpoint × (wall time, query count) before/after.
2. **What we changed** — file list with one-line justifications, mirroring the structure of the DX section.
3. **What we deliberately didn't do** — schema changes, search backend, auth, prod-readiness.
4. **What we'd do next** — the Phase 4 list endpoints, plus a hint that `get_post` could be a single query with denormalization if traffic justifies it.

Commit and push to a new branch `perf/round-1`, open PR #2.

---

## Phase 6 — transcript (the rule we set in the previous turn)

When the round is done and we're about to close, dump the conversation from this turn onwards to `ai-transcriptions/performance_round_1.txt`. Use the same formatter we wrote last time (or a fresh inline one). The previous transcript stops at the merge-to-main turn; this new file picks up from there and goes through the close of the PR.

---

## Out of scope (explicit, to avoid creep)

- Authentication / authorization (the README says skip).
- Test coverage as a goal.
- Production readiness / Helm / ECS / Fly.
- A real search backend.
- Denormalization, materialized views, or any model-definition change.
- Caching layer (Redis). We will mention it in `NOTES.md` under "What we'd do next" if it shows up as a bottleneck.

## Risks I'm flagging up front

- **Seed time on this machine** could blow past 5 min. If it does, we drop `NUM_COMMENTS` to 100k for this round (a setting we can override in a local `.env`-driven `seed.py` argument) and note the deviation in `NOTES.md`. We do not change the committed seed constants.
- **`pg_trgm` extension creation** may fail if the DB role lacks privilege. Fallback: install it manually from a privileged connection before running the migration.
- **Pagination change is technically a breaking change** to the API contract. `NOTES.md` will be explicit: this was discussed, approved, and is the right call for an unbounded list.
