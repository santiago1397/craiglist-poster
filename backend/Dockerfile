# Multi-stage build. Build context is the REPO ROOT (see docker-compose.prod.yml),
# which lets us COPY src/craigslist_auto/events.py into the backend so the
# shared event schema comes from a single source of truth.

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# ---- deps stage ----
FROM base AS deps
WORKDIR /app

# Fonts for the cover overlay. python:3.12-slim ships with NO font files at all
# — not one .ttf anywhere on the filesystem — and Pillow does not bundle any for
# runtime use. Without this, `overlay._font()` falls through to
# `ImageFont.load_default()`, a fixed ~11px bitmap face that ignores the size it
# is given, so every composited cover goes out with a phone number too small to
# read. That was true for the entire life of this system before it was caught.
RUN apt-get update && \
    apt-get install -y --no-install-recommends fonts-dejavu-core && \
    rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml ./
RUN pip install --upgrade pip && \
    pip install \
        "fastapi>=0.115.0" \
        "uvicorn[standard]>=0.32.0" \
        "pydantic[email]>=2.9.0" \
        "pydantic-settings>=2.6.0" \
        "psycopg[binary,pool]>=3.2.0" \
        "argon2-cffi>=23.1.0" \
        "python-jose[cryptography]>=3.3.0" \
        "python-multipart>=0.0.12" \
        "slowapi>=0.1.9" \
        "alembic>=1.14.0" \
        "python-dotenv>=1.0.1" \
        "loguru>=0.7.2" \
        "httpx>=0.27.2" \
        "pillow>=10.4.0" \
        "sqlalchemy>=2.0.0"

# ---- runtime stage ----
FROM deps AS runtime
WORKDIR /app

# Copy backend code first
COPY backend/app ./app
COPY backend/alembic ./alembic
COPY backend/alembic.ini ./alembic.ini
COPY backend/pyproject.toml ./pyproject.toml

# Overwrite the schemas file with the single source of truth from src/.
# This is the whole reason the build context is the repo root.
COPY src/craigslist_auto/events.py ./app/schemas/events.py

# Non-root user
RUN useradd -m -u 10001 appuser

# Create the image-storage mount point *before* dropping privileges and before
# the volume is first attached. Docker seeds a new named volume from the image's
# directory, ownership included — without this the volume arrives owned by root
# and the non-root process cannot write to it.
RUN mkdir -p /var/lib/cl/images && chown -R appuser:appuser /var/lib/cl

USER appuser

EXPOSE 8000

# Migrations run on startup; failing here should crash the container so
# Traefik/health-check picks it up.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 2 --proxy-headers"]
