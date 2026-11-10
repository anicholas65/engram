# syntax=docker/dockerfile:1

# Engram — Linux container image.
#
# Three stages: build the React frontend, resolve the Python venv, then assemble
# a Debian runtime. MakeMKV is intentionally NOT baked in — it is compiled into a
# persistent volume on first start (see docker/install-makemkv.sh) so this image
# never redistributes MakeMKV binaries.

# ---------------------------------------------------------------------------
# Stage 1: frontend build (Vite -> static SPA)
# ---------------------------------------------------------------------------
FROM node:24-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# vite.config.ts reads the app version from the backend package (../backend/app/__init__.py).
COPY backend/app/__init__.py /build/backend/app/__init__.py
RUN npm run build
# Output: /build/frontend/dist

# ---------------------------------------------------------------------------
# Stage 2: Python dependency resolution (uv -> .venv)
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS backend-builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Resolve deps first (cached unless the lockfile changes). The project itself is
# not installed (no [build-system]); it runs from source — mirrors CI's
# `uv sync --no-install-project`.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
# Output: /app/.venv

# ---------------------------------------------------------------------------
# Stage 3: runtime
# ---------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# Runtime tools + MakeMKV build toolchain (needed for the first-run compile) +
# gosu (privilege drop). The toolchain inflates the image but is the cost of
# keeping MakeMKV out of the published layers.
#
# Fork (encode stage): ffmpeg comes from jellyfin-ffmpeg7 instead of Debian's 5.1,
# so hevc_nvenc writes HDR10 mastering-display / MaxCLL metadata and NVENC/NVDEC
# match what Jellyfin on the same GPU uses. It installs to /usr/lib/jellyfin-ffmpeg;
# the symlinks in /usr/local/bin put it first on PATH for every caller.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && install -d -m 0755 /etc/apt/keyrings \
    && curl -fsSL https://repo.jellyfin.org/jellyfin_team.gpg.key -o /etc/apt/keyrings/jellyfin.asc \
    && printf '%s\n' \
        'Types: deb' \
        'URIs: https://repo.jellyfin.org/debian' \
        'Suites: bookworm' \
        'Components: main' \
        'Architectures: amd64' \
        'Signed-By: /etc/apt/keyrings/jellyfin.asc' \
        > /etc/apt/sources.list.d/jellyfin.sources \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y --no-install-recommends \
        jellyfin-ffmpeg7 \
        libsndfile1 \
        util-linux \
        eject \
        ca-certificates \
        curl \
        gosu \
        build-essential \
        pkg-config \
        libc6-dev \
        libssl-dev \
        libexpat1-dev \
        libavcodec-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/lib/jellyfin-ffmpeg/ffmpeg /usr/local/bin/ffmpeg \
    && ln -sf /usr/lib/jellyfin-ffmpeg/ffprobe /usr/local/bin/ffprobe \
    && ffmpeg -hide_banner -version | head -n1 \
    && ffmpeg -hide_banner -encoders | grep -q hevc_nvenc

WORKDIR /app

# Python venv + application source.
COPY --from=backend-builder /app/.venv /app/.venv
COPY backend/ /app/
# Bundled SPA. main.py serves it from "<main.py dir>/static" -> /app/app/static.
COPY --from=frontend-builder /build/frontend/dist /app/app/static

# Entrypoint + MakeMKV installer.
COPY docker/entrypoint.sh docker/install-makemkv.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/install-makemkv.sh

# NVIDIA_DRIVER_CAPABILITIES (fork, encode stage): with --runtime=nvidia this
# exposes NVENC/NVDEC to ffmpeg; ignored when the NVIDIA runtime is not used.
ENV PATH="/app/.venv/bin:$PATH" \
    HOME=/config \
    HF_HOME=/config/.cache/huggingface \
    DATABASE_URL="sqlite+aiosqlite:////config/.engram/engram.db" \
    HOST=0.0.0.0 \
    PORT=8000 \
    DEBUG=false \
    PUID=1000 \
    PGID=1000 \
    MAKEMKV_VERSION=latest \
    NVIDIA_DRIVER_CAPABILITIES=compute,video,utility

EXPOSE 8000
VOLUME ["/config"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
