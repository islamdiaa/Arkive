# =============================================================================
# Arkive — Multi-stage Dockerfile
# Stage 1: Build SvelteKit frontend with Node 22
# Stage 2: Build restic + rclone from source with a patched Go toolchain
# Stage 3: Python 3.12 runtime (multi-arch)
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1 — Frontend Builder
# ---------------------------------------------------------------------------
FROM node:22-alpine AS frontend-builder
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ .
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2 — restic + rclone
# ---------------------------------------------------------------------------
# Built from the release tags (checked against their commits) rather than
# downloaded: the published binaries lag behind Go security releases, and the
# release workflow's Trivy scan fails on fixable HIGH/CRITICAL findings. The
# restic dependency bumps below fix advisories in its pinned modules; drop
# them when a restic release includes them.
FROM --platform=$BUILDPLATFORM golang:1.26.10-bookworm AS tools-builder
ARG TARGETARCH
ARG RESTIC_VERSION=0.19.1
ARG RESTIC_COMMIT=6aa3a516ce654808a1f28f9fa21e9b7c8e6e90bf
ARG RCLONE_VERSION=1.75.2
ARG RCLONE_COMMIT=be8d6044eacb843b74d335db0c5fbe09222031fe
ENV CGO_ENABLED=0 GOOS=linux GOTOOLCHAIN=local GOFLAGS=-trimpath
WORKDIR /src
RUN git clone --depth 1 --branch "v${RESTIC_VERSION}" https://github.com/restic/restic.git && \
    cd restic && test "$(git rev-parse HEAD)" = "${RESTIC_COMMIT}" && \
    go get github.com/klauspost/compress@v1.18.7 golang.org/x/crypto@v0.57.0 \
      golang.org/x/net@v0.60.0 golang.org/x/text@v0.42.0 google.golang.org/grpc@v1.83.2 && \
    go mod tidy && \
    GOARCH="${TARGETARCH}" go build -tags disable_grpc_modules \
      -ldflags "-s -w -X main.version=${RESTIC_VERSION}" -o /out/restic ./cmd/restic
RUN git clone --depth 1 --branch "v${RCLONE_VERSION}" https://github.com/rclone/rclone.git && \
    cd rclone && test "$(git rev-parse HEAD)" = "${RCLONE_COMMIT}" && \
    GOARCH="${TARGETARCH}" go build -tags noselfupdate \
      -ldflags "-s -w -X github.com/rclone/rclone/fs.Version=v${RCLONE_VERSION}" -o /out/rclone .

# ---------------------------------------------------------------------------
# Stage 3 — Python Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
ARG TARGETARCH
ARG VERSION=0.4.0

# OCI image metadata
LABEL org.opencontainers.image.title="Arkive" \
      org.opencontainers.image.description="Automated disaster recovery for Unraid servers" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/kemetlabs/Arkive" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="Arkive" \
      maintainer="kemetlabs"

# System dependencies (includes WeasyPrint requirements: pango, cairo, gdk-pixbuf).
# The upgrade picks up Debian security fixes newer than the base image.
RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
    sqlite3 curl tini ca-certificates \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
    libffi-dev libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# restic and rclone, built in the stage above
COPY --from=tools-builder /out/restic /out/rclone /usr/local/bin/

# Python dependencies (use uv for fast installs)
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install uv && uv pip install --system --no-cache -r requirements.txt

# Copy application
COPY backend/ /app/backend/
COPY profiles/ /app/profiles/

# Copy built frontend into backend static directory for serving
COPY --from=frontend-builder /build/build/ /app/frontend/build/

# Set PYTHONPATH so `from app.xxx` imports resolve correctly
ENV PYTHONPATH=/app/backend \
    PYTHONUNBUFFERED=1 \
    ARKIVE_VERSION=${VERSION}

# Create non-root user and set ownership
RUN groupadd --system arkive && useradd --system --gid arkive --create-home arkive \
    && chown -R arkive:arkive /app
RUN mkdir -p /config /cache /data && chown -R arkive:arkive /config /cache /data

# Volumes and ports
VOLUME ["/config", "/data", "/cache"]
EXPOSE 8200

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -sf http://localhost:8200/api/status || exit 1

USER arkive

# Entry point with tini for proper signal handling
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200", "--workers", "1", "--no-server-header", "--log-config", "/app/backend/logging.json"]
