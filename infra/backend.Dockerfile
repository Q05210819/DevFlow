FROM ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 AS uv
FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend/src ./src
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./
# Editable installation deliberately preserves db.py's src-relative migration path.
RUN uv sync --locked --no-dev

FROM python:3.12.13-slim-bookworm@sha256:4766d8b510c428e595d74b9cc5bbb2fae8e26316fffb4adc89908d79aacd58a2 AS runtime
COPY --from=uv /uv /usr/local/bin/uv
RUN groupadd --gid 10001 devflow && useradd --uid 10001 --gid 10001 --create-home devflow
WORKDIR /app/backend
COPY --from=build /app/backend /app/backend
ENV UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
# Compose mounts the URL as a secret; never persist it in image or container config.
ENTRYPOINT ["sh", "-eu", "-c", "if [ -f /run/secrets/database_url ]; then DATABASE_URL=\"$(cat /run/secrets/database_url)\"; export DATABASE_URL; fi; exec \"$@\"", "--"]
CMD ["uv", "run", "--no-sync", "uvicorn", "devflow.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio:SelectorEventLoop", "--no-access-log"]
