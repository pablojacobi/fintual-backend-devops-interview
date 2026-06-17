# syntax=docker/dockerfile:1.7

# ---- builder ---------------------------------------------------------------
# Resolves runtime deps via uv and runs `collectstatic` so the runtime
# image ships with the static files baked in. Uses the dev settings
# module because it has the same STATIC_URL/STATIC_ROOT contract as prod
# and we just need the files on disk; the runtime image then sets
# DJANGO_SETTINGS_MODULE=core.settings_prod.
FROM python:3.14-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/venv \
    PATH="/venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .

# Bake static files into the image. We need a writable STATIC_ROOT and a
# permissive ALLOWED_HOSTS for the management command, but the actual
# settings used at runtime are core.settings_prod.
RUN DJANGO_SETTINGS_MODULE=core.settings \
    DJANGO_ALLOWED_HOSTS=* \
    DJANGO_SECRET_KEY=collectstatic-tmp \
    mkdir -p /app/staticfiles \
    && python manage.py collectstatic --noinput

# ---- runtime ---------------------------------------------------------------
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=core.settings_prod

# Same uv install as the builder so the runtime can use the venv at
# /venv (copying it directly from the builder stage is faster than
# reinstalling).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

# Non-root user. Matches the gid/uid conventions of the official Python
# images. /app is owned by app:app so collectstatic and any file
# operations don't need root.
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home /app --shell /usr/sbin/nologin app \
    && mkdir -p /app /app/staticfiles \
    && chown -R app:app /app

WORKDIR /app

# Copy the venv (with the runtime deps already resolved) and the project
# from the builder. /app/staticfiles is already populated.
COPY --from=builder --chown=app:app /venv /venv
COPY --from=builder --chown=app:app /app /app

USER app

EXPOSE 8000

# `exec` so gunicorn is PID 1 and SIGTERM (from `docker stop`) reaches
# it directly. gunicorn handles graceful shutdown of its workers on
# SIGTERM; the 30s default timeout in gunicorn.conf.py gives them
# enough headroom to finish in-flight requests.
CMD ["gunicorn", "core.wsgi:application", "-c", "gunicorn.conf.py"]
