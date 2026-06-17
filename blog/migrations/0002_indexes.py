from django.contrib.postgres.operations import CreateExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Add indexes for the read paths exercised by the API.

    The Phase 2 baseline (see PERFORMANCE_PLAN.md) showed:

    - `list_posts` did a Parallel Seq Scan on blog_post because no index
      supported `WHERE is_published=true ORDER BY created_at DESC`.

    - `search_posts` did a Seq Scan on `ILIKE '%x%'`, which Postgres can
      only accelerate with a trigram GIN index. The `pg_trgm` extension
      provides `gin_trgm_ops`.

    - `posts_by_tag` already had an index on `(tag_id)`, but the sort
      `ORDER BY created_at DESC` for the matched posts then needed a
      re-sort. A composite `(is_published, created_at DESC)` on blog_post
      covers this too (Postgres walks it backwards).

    - `get_post` lists comments ordered by `created_at`; the existing
      FK index on `(post_id)` is fine for the lookup but a composite
      `(post_id, created_at)` avoids a sort on every call.

    All the read endpoints filter on `is_published=true`, so the three
    blog_post indexes are partial. That keeps them small (no rows for
    drafts), reduces write amplification, and fits more in the page
    cache. In particular the GIN trigram on `body` is large and
    expensive to maintain; restricting it to published rows is a
    meaningful win.

    No model definitions change; only SQL via RunSQL so the migration is
    reversible.
    """

    dependencies = [("blog", "0001_initial")]

    operations = [
        CreateExtension("pg_trgm"),
        migrations.RunSQL(
            "CREATE INDEX blog_post_pub_created_idx "
            "ON blog_post (created_at DESC) WHERE is_published = true",
            reverse_sql="DROP INDEX blog_post_pub_created_idx",
        ),
        migrations.RunSQL(
            "CREATE INDEX blog_post_title_trgm "
            "ON blog_post USING gin (title gin_trgm_ops) "
            "WHERE is_published = true",
            reverse_sql="DROP INDEX blog_post_title_trgm",
        ),
        migrations.RunSQL(
            "CREATE INDEX blog_post_body_trgm "
            "ON blog_post USING gin (body gin_trgm_ops) "
            "WHERE is_published = true",
            reverse_sql="DROP INDEX blog_post_body_trgm",
        ),
        migrations.RunSQL(
            "CREATE INDEX blog_comment_post_created_idx "
            "ON blog_comment (post_id, created_at)",
            reverse_sql="DROP INDEX blog_comment_post_created_idx",
        ),
    ]
