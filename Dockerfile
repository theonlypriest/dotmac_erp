# syntax=docker/dockerfile:1.7

# Compile the committed stylesheet without carrying Node into the runtime image.
FROM node:20-alpine AS css-builder

WORKDIR /build

COPY package.json package-lock.json* ./
RUN npm ci --silent

COPY tailwind.config.js postcss.config.js ./
COPY src/css ./src/css
# EVERY path in tailwind.config.js `content` must be present here, or Tailwind
# silently emits a stylesheet missing the classes it could not see. `static/js`
# was absent, so the image's app.css omitted every class used only from
# JavaScript -- status badges, toasts, dynamic highlighting. That defect was
# MASKED in production by the compose bind mount of ./static, which shadowed the
# image's stylesheet with the checkout's fully-built one. Removing that mount
# without this line would have traded one stale-stylesheet defect for a new one.
# tests/architecture/test_static_is_image_only.py holds the two in step.
COPY templates ./templates
COPY static/js ./static/js

RUN npm run build:css


# Resolve the exact production lock into an isolated application virtualenv.
# Poetry is builder tooling: it never enters the final image.
FROM python:3.12-slim AS dependency-builder

ARG POETRY_VERSION=2.4.1

ENV POETRY_NO_INTERACTION=1 \
    POETRY_VIRTUALENVS_CREATE=0 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /build

RUN python -m venv /opt/venv \
    && /usr/local/bin/python -m pip install --no-cache-dir "poetry==${POETRY_VERSION}"

COPY pyproject.toml poetry.lock ./

# Dotmac packages resolve from the private Forgejo index. The read token is a
# BuildKit secret, so neither its value nor an authenticated URL enters a layer.
RUN --mount=type=secret,id=forgejo_token,required=true \
    POETRY_HTTP_BASIC_FORGEJO_USERNAME=ci-reader \
    POETRY_HTTP_BASIC_FORGEJO_PASSWORD="$(cat /run/secrets/forgejo_token)" \
    poetry install --only main --no-root --no-ansi


# The production image contains runtime libraries, the locked application
# virtualenv and named runtime surfaces only. Source metadata, tests, Poetry,
# Node and the rest of the repository never enter this stage.
FROM python:3.12-slim AS runtime

ENV HOME=/home/dotmac \
    PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/tmp \
    VIRTUAL_ENV=/opt/venv \
    XDG_CACHE_HOME=/tmp/.cache

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libglib2.0-0 \
        libpango-1.0-0 \
        libpangocairo-1.0-0 \
        libffi8 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 dotmac \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --create-home \
        --home-dir /home/dotmac \
        --shell /usr/sbin/nologin \
        dotmac \
    && mkdir -p /app/license

COPY --from=dependency-builder /opt/venv /opt/venv

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini
COPY gunicorn.conf.py ./gunicorn.conf.py
COPY locales ./locales
COPY templates ./templates
COPY static ./static
# The licence directory was previously supplied ONLY by a bind mount of the
# source checkout, so removing that mount would have removed /app/license
# entirely -- and LICENSE_FILE_PATH defaults to /app/license/dotmac.lic
# (app/config.py, app/licensing/enforcement.py). The image now owns the
# directory. A real licence is deployment material and must arrive as a
# declared material path, never from the source tree.
COPY license ./license
# Named operator entrypoints only; the rest of scripts/ is not part of the
# runtime surface. The deploy invokes both of these inside this image
# (scripts/deploy.sh steps 3a and 3c) and nothing mounts the checkout over
# /app, so an entrypoint missing here is unreachable at deploy time.
COPY scripts/bootstrap_database_roles.py ./scripts/bootstrap_database_roles.py
COPY scripts/verify_runtime_admission.py ./scripts/verify_runtime_admission.py
COPY scripts/probe_people_employment_type_activation.py ./scripts/probe_people_employment_type_activation.py

# Compiled CSS is copied last so a stale source-tree stylesheet cannot replace
# the builder output.
COPY --from=css-builder /build/static/css/app.css ./static/css/app.css

EXPOSE 8002

USER 10001:10001

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app.main:app"]
