import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify
from faker import Faker

from blog.models import Comment, Post, Tag, User

NUM_USERS = 1000
NUM_TAGS = 50
NUM_POSTS = 100_000
NUM_COMMENTS = 500_000
TAGS_PER_POST_AVG = 3
TITLE_POOL_SIZE = 10_000
BODY_POOL_SIZE = 10_000
BATCH = 1000


class Command(BaseCommand):
    help = "Seed the database with users, tags, posts, and comments."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Seed even if data exists")

    def handle(self, *args, **opts):
        if User.objects.exists() and not opts["force"]:
            self.stdout.write("Database already has users; pass --force to seed anyway.")
            return

        fake = Faker()
        Faker.seed(42)
        random.seed(42)

        now = timezone.now()
        three_years_ago = now - timedelta(days=365 * 3)

        self.stdout.write("Seeding users...")
        users = [
            User(
                username=f"user{i:05d}",
                email=f"user{i:05d}@example.com",
                display_name=fake.name(),
                bio=fake.text(max_nb_chars=200) if i % 4 == 0 else "",
                created_at=_random_time(three_years_ago, now),
            )
            for i in range(NUM_USERS)
        ]
        with transaction.atomic():
            User.objects.bulk_create(users, batch_size=BATCH)
        users = list(User.objects.all().only("id"))
        user_ids = [u.id for u in users]

        self.stdout.write("Seeding tags...")
        hot_slugs = ["python", "django", "postgres", "devops", "sre"]
        tag_objs = [Tag(name=s.title(), slug=s, created_at=now) for s in hot_slugs]
        for _ in range(NUM_TAGS - len(hot_slugs)):
            word = fake.unique.word()
            tag_objs.append(Tag(name=word.title(), slug=slugify(word), created_at=now))
        with transaction.atomic():
            Tag.objects.bulk_create(tag_objs, batch_size=BATCH)
        tags = list(Tag.objects.all().only("id", "slug"))
        hot_tag_ids = [t.id for t in tags if t.slug in hot_slugs]
        cold_tag_ids = [t.id for t in tags if t.slug not in hot_slugs]

        title_pool = [fake.sentence(nb_words=8).rstrip(".") for _ in range(TITLE_POOL_SIZE)]
        body_pool = [fake.text(max_nb_chars=600) for _ in range(BODY_POOL_SIZE)]

        author_weights = _power_law_weights(len(user_ids), top_n=10, top_share=0.3)

        self.stdout.write(f"Seeding {NUM_POSTS} posts...")
        recent_days = 180
        recency_cutoff = now - timedelta(days=recent_days)
        with transaction.atomic():
            for chunk_start in range(0, NUM_POSTS, BATCH):
                chunk = []
                for _ in range(chunk_start, min(chunk_start + BATCH, NUM_POSTS)):
                    if random.random() < 0.5:
                        ts = _random_time(recency_cutoff, now)
                    else:
                        ts = _random_time(three_years_ago, now)
                    author_id = random.choices(user_ids, weights=author_weights, k=1)[0]
                    chunk.append(
                        Post(
                            author_id=author_id,
                            title=random.choice(title_pool),
                            body=random.choice(body_pool),
                            is_published=random.random() < 0.9,
                            view_count=random.randint(0, 5000),
                            created_at=ts,
                        )
                    )
                Post.objects.bulk_create(chunk, batch_size=BATCH)

        post_ids = list(Post.objects.values_list("id", flat=True))

        self.stdout.write("Attaching tags to posts...")
        through = Post.tags.through
        m2m_rows = []
        for pid in post_ids:
            n_tags = max(1, int(random.gauss(TAGS_PER_POST_AVG, 1)))
            chosen = set()
            for _ in range(n_tags):
                if random.random() < 0.4 and hot_tag_ids:
                    chosen.add(random.choice(hot_tag_ids))
                else:
                    chosen.add(random.choice(cold_tag_ids))
            for tid in chosen:
                m2m_rows.append(through(post_id=pid, tag_id=tid))
            if len(m2m_rows) >= BATCH * 10:
                with transaction.atomic():
                    through.objects.bulk_create(m2m_rows, batch_size=BATCH, ignore_conflicts=True)
                m2m_rows = []
        if m2m_rows:
            with transaction.atomic():
                through.objects.bulk_create(m2m_rows, batch_size=BATCH, ignore_conflicts=True)

        self.stdout.write(f"Seeding {NUM_COMMENTS} comments...")
        post_weights = _long_tail_weights(len(post_ids), top_pct=0.01, top_share=0.5)
        for chunk_start in range(0, NUM_COMMENTS, BATCH):
            chunk = []
            for _ in range(chunk_start, min(chunk_start + BATCH, NUM_COMMENTS)):
                pid = random.choices(post_ids, weights=post_weights, k=1)[0]
                aid = random.choices(user_ids, weights=author_weights, k=1)[0]
                chunk.append(
                    Comment(
                        post_id=pid,
                        author_id=aid,
                        body=fake.sentence(nb_words=random.randint(5, 30)),
                        created_at=_random_time(three_years_ago, now),
                    )
                )
            Comment.objects.bulk_create(chunk, batch_size=BATCH)

        self.stdout.write(self.style.SUCCESS("Done."))


def _random_time(start, end):
    delta = end - start
    return start + timedelta(seconds=random.randint(0, int(delta.total_seconds())))


def _power_law_weights(n, top_n, top_share):
    weights = [1.0] * n
    bonus = (top_share * n) / max(top_n, 1)
    for i in range(min(top_n, n)):
        weights[i] = 1.0 + bonus
    return weights


def _long_tail_weights(n, top_pct, top_share):
    weights = [1.0] * n
    top_n = max(1, int(n * top_pct))
    bonus = (top_share * n) / top_n
    for i in range(top_n):
        weights[i] = 1.0 + bonus
    return weights
