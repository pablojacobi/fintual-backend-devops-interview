from django.db.models import F, Prefetch, Q
from django.shortcuts import get_object_or_404
from ninja import Router

from blog.models import Comment, Post, Tag, User
from blog.schemas import (
    CommentCreateIn,
    CommentCreateOut,
    PostCreateIn,
    PostCreateOut,
    PostDetailOut,
    PostListOut,
    UserDetailOut,
)

router = Router()

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(value, hi))


def _serialize_author(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
    }


def _serialize_tag(tag: Tag) -> dict:
    return {"id": tag.id, "name": tag.name, "slug": tag.slug}


def _serialize_post_list(post: Post) -> dict:
    return {
        "id": post.id,
        "title": post.title,
        "author": _serialize_author(post.author),
        "tags": [_serialize_tag(t) for t in post.tags.all()],
        "view_count": post.view_count,
        "created_at": post.created_at,
    }


@router.get("/posts", response=list[PostListOut])
def list_posts(request, limit: int = DEFAULT_LIMIT, offset: int = 0):
    limit = _clamp(limit, 1, MAX_LIMIT)
    offset = max(0, offset)
    posts = (
        Post.objects.select_related("author")
        .prefetch_related("tags")
        .filter(is_published=True)
        .order_by("-created_at")[offset : offset + limit]
    )
    return [_serialize_post_list(p) for p in posts]


@router.get("/posts/search", response=list[PostListOut])
def search_posts(request, q: str, limit: int = DEFAULT_LIMIT, offset: int = 0):
    limit = _clamp(limit, 1, MAX_LIMIT)
    offset = max(0, offset)
    posts = (
        Post.objects.select_related("author")
        .prefetch_related("tags")
        .filter(
            Q(title__icontains=q) | Q(body__icontains=q),
            is_published=True,
        )
        .order_by("-created_at")[offset : offset + limit]
    )
    return [_serialize_post_list(p) for p in posts]


@router.get("/posts/by-tag/{slug}", response=list[PostListOut])
def posts_by_tag(request, slug: str, limit: int = DEFAULT_LIMIT, offset: int = 0):
    tag = get_object_or_404(Tag, slug=slug)
    limit = _clamp(limit, 1, MAX_LIMIT)
    offset = max(0, offset)
    posts = (
        tag.posts.select_related("author")
        .prefetch_related("tags")
        .filter(is_published=True)
        .order_by("-created_at")[offset : offset + limit]
    )
    return [_serialize_post_list(p) for p in posts]


@router.get("/posts/{post_id}", response=PostDetailOut)
def get_post(request, post_id: int):
    post = get_object_or_404(
        Post.objects.select_related("author").prefetch_related(
            "tags",
            Prefetch(
                "comments",
                queryset=Comment.objects.select_related("author").order_by("created_at"),
            ),
        ),
        id=post_id,
    )
    # Atomic at the DB level: two concurrent reads of `post.view_count`
    # can both be stale, but `F("view_count") + 1` becomes a single SQL
    # expression that the database applies under the row's lock.
    Post.objects.filter(id=post_id).update(view_count=F("view_count") + 1)

    comments = [
        {
            "id": c.id,
            "author": _serialize_author(c.author),
            "body": c.body,
            "created_at": c.created_at,
        }
        for c in post.comments.all()
    ]
    return {
        "id": post.id,
        "title": post.title,
        "body": post.body,
        "author": _serialize_author(post.author),
        "tags": [_serialize_tag(t) for t in post.tags.all()],
        "comments": comments,
        "view_count": post.view_count + 1,
        "created_at": post.created_at,
        "updated_at": post.updated_at,
    }


@router.post("/posts", response=PostCreateOut)
def create_post(request, payload: PostCreateIn):
    author = get_object_or_404(User, id=payload.author_id)
    post = Post.objects.create(
        author=author,
        title=payload.title,
        body=payload.body,
    )
    for slug in payload.tag_slugs:
        tag = Tag.objects.get(slug=slug)
        post.tags.add(tag)
    return {"id": post.id, "title": post.title}


@router.post("/posts/{post_id}/comments", response=CommentCreateOut)
def create_comment(request, post_id: int, payload: CommentCreateIn):
    post = get_object_or_404(Post, id=post_id)
    author = get_object_or_404(User, id=payload.author_id)
    comment = Comment.objects.create(post=post, author=author, body=payload.body)
    return {"id": comment.id}


@router.get("/users/find", response=UserDetailOut)
def find_user_by_email(request, email: str):
    user = get_object_or_404(User, email=email)
    return _user_detail(user)


@router.get("/users/{user_id}", response=UserDetailOut)
def get_user(request, user_id: int):
    user = get_object_or_404(User, id=user_id)
    return _user_detail(user)


def _user_detail(user: User) -> dict:
    # Two `count(*)` queries with `Index Only Scan` (one on blog_post,
    # one on blog_comment) beat a single `Count` annotation: the latter
    # forces a JOIN that is O(posts * comments) for the matched user.
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "email": user.email,
        "bio": user.bio,
        "post_count": user.posts.count(),
        "comment_count": user.comments.count(),
    }
