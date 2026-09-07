# ── Stage 1: Build the SvelteKit frontend ──────────────────
FROM --platform=$BUILDPLATFORM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS frontend-builder

WORKDIR /build/frontend
COPY cptr/frontend/package.json cptr/frontend/package-lock.json ./
RUN npm ci
COPY cptr/frontend/ ./
RUN npm run build


# ── Stage 2: Install Python dependencies & build wheel ─────
FROM --platform=$BUILDPLATFORM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS backend-builder

WORKDIR /build
COPY pyproject.toml uv.lock LICENSE README.md CHANGELOG.md ./
COPY cptr/ cptr/

# Drop the pre-built frontend into the package tree
COPY --from=frontend-builder /build/frontend/build cptr/frontend/build

# Export the exact locked runtime graph (including optional production features)
# and build the wheel. Runtime installation below consumes only these frozen hashes.
RUN uv export --frozen --all-extras --no-dev --no-emit-project \
        --format requirements-txt --output-file /dist/runtime-requirements.txt && \
    uv build --wheel --out-dir /dist


# ── Stage 3: Shared runtime ────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS runtime

LABEL org.opencontainers.image.source="https://github.com/open-webui/computer"
LABEL org.opencontainers.image.description="Open WebUI Computer"

# Runtime deps shared by default and browser images.
RUN apt-get update && \
    apt-get install -y --no-install-recommends gh git tini && \
    rm -rf /var/lib/apt/lists/*

# Create non-root user and writable data directory
RUN useradd --create-home --shell /bin/bash cptr && \
    mkdir -p /data && \
    chown -R cptr:cptr /data
USER cptr
WORKDIR /home/cptr

# Install exactly the dependency graph captured by uv.lock, then install the
# project wheel without allowing a second dependency resolution.
COPY --chown=cptr:cptr --from=backend-builder /dist/ /tmp/cptr-dist/
RUN uv venv /home/cptr/.venv && \
    uv pip install --python /home/cptr/.venv/bin/python \
        --require-hashes -r /tmp/cptr-dist/runtime-requirements.txt && \
    set -- /tmp/cptr-dist/*.whl && \
    uv pip install --python /home/cptr/.venv/bin/python --no-deps "$1" && \
    rm -rf /tmp/cptr-dist

ENV PATH="/home/cptr/.venv/bin:$PATH"
ENV CPTR_DATA_DIR="/data"

EXPOSE 8000
VOLUME ["/data"]

ENTRYPOINT ["tini", "--"]
CMD ["cptr", "run", "--host", "0.0.0.0", "--port", "8000", "--headless"]


# ── Browser image: Chromium for agent browser automation ───
FROM runtime AS browser

USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends chromium && \
    rm -rf /var/lib/apt/lists/*
USER cptr


# ── Default image ──────────────────────────────────────────
FROM runtime AS default
