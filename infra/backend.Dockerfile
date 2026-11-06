FROM ghcr.io/astral-sh/uv:0.11.25@sha256:1e3808aa9023d0980e7c15b1fa7c1ac16ff35925780cf5c459858b2d693f01a9 AS uv
FROM python:3.12.13-slim-trixie@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b AS build
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

# Model inference is an explicit optional extra, installed only for the model
# worker image. The API and ordinary worker stages must never import torch.
FROM build AS build-models
RUN uv sync --locked --no-dev --extra models

FROM python:3.12.13-slim-trixie@sha256:646fb0bca3dd3ea1bcc6feb72c17ed16eed6e10cffc732fcc1478bd3e7f02d7b AS runtime
COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update && apt-get install --no-install-recommends -y git ca-certificates \
    && dpkg --compare-versions "$(git --version | cut -d ' ' -f 3)" ge 2.43 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 devflow \
    && useradd --uid 10001 --gid 10001 --create-home devflow \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/devflow/workspace
WORKDIR /app/backend
COPY --from=build /app/backend /app/backend
ENV UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
EXPOSE 8000
# Compose mounts the URL as a secret; never persist it in image or container config.
ENTRYPOINT ["sh", "-eu", "-c", "if [ -f /run/secrets/database_url ]; then DATABASE_URL=\"$(cat /run/secrets/database_url)\"; export DATABASE_URL; fi; exec \"$@\"", "--"]
CMD ["uv", "run", "--no-sync", "uvicorn", "devflow.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--loop", "asyncio:SelectorEventLoop", "--no-access-log"]

# Dedicated model worker image. Keep it separate from `runtime` so the API and
# ordinary worker image never ships the optional torch/sentence-transformers
# dependencies. The base runtime still carries the 3A tokenizer dependencies.
FROM runtime AS model-runtime
USER root
# CPU torch wheels load libgomp at import; slim images do not provide it.
RUN apt-get update && apt-get install --no-install-recommends -y libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o 10001 -g 10001 -m 0750 /var/lib/devflow/model
COPY --chown=10001:10001 --from=build-models /app/backend /app/backend
USER 10001:10001
CMD ["uv", "run", "--no-sync", "python", "-m", "devflow.worker"]
