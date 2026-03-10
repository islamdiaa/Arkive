# =============================================================================
# Arkive — Multi-stage Dockerfile
# Stage 1: Build SvelteKit frontend with Node 22
# Stage 2: Python 3.12 runtime with restic + rclone (multi-arch)
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
# Stage 2 — Python Runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime
ARG TARGETARCH

# OCI image metadata
LABEL org.opencontainers.image.title="Arkive" \
      org.opencontainers.image.description="Automated disaster recovery for Unraid servers" \
      org.opencontainers.image.version="0.1.0" \
      org.opencontainers.image.source="https://github.com/islamdiaa/Arkive" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.vendor="Arkive" \
      maintainer="islamdiaa"

# System dependencies (includes WeasyPrint requirements: pango, cairo, gdk-pixbuf)
RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 curl tini ca-certificates bzip2 unzip \
    libpango-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 \
    libffi-dev libglib2.0-0 && \
    rm -rf /var/lib/apt/lists/*

# Download restic binary for target arch
RUN ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" && \
    case "${ARCH}" in \
      amd64) RESTIC_ARCH=amd64; RESTIC_SHA256=5097faeda6aa13167aae6e36efdba636637f8741fed89bbf015678334632d4d3 ;; \
      arm64) RESTIC_ARCH=arm64; RESTIC_SHA256=db27b803534d301cef30577468cf61cb2e242165b8cd6d8cd6efd7001be2e557 ;; \
      *) echo "Unsupported architecture: ${ARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fL -o /tmp/restic.bz2 \
      "https://github.com/restic/restic/releases/download/v0.17.3/restic_0.17.3_linux_${RESTIC_ARCH}.bz2" && \
    echo "${RESTIC_SHA256} /tmp/restic.bz2" | sha256sum -c - && \
    bunzip2 /tmp/restic.bz2 && \
    mv /tmp/restic /usr/local/bin/restic && \
    chmod +x /usr/local/bin/restic

# Download rclone binary for target arch
RUN ARCH="${TARGETARCH:-$(dpkg --print-architecture)}" && \
    case "${ARCH}" in \
      amd64) RCLONE_ARCH=amd64; RCLONE_SHA256=0e6fa18051e67fc600d803a2dcb10ddedb092247fc6eee61be97f64ec080a13c ;; \
      arm64) RCLONE_ARCH=arm64; RCLONE_SHA256=c6e9d4cf9c88b279f6ad80cd5675daebc068e404890fa7e191412c1bc7a4ac5f ;; \
      *) echo "Unsupported architecture: ${ARCH}" >&2; exit 1 ;; \
    esac && \
    curl -fL -o /tmp/rclone.zip \
      "https://downloads.rclone.org/v1.68.2/rclone-v1.68.2-linux-${RCLONE_ARCH}.zip" && \
    echo "${RCLONE_SHA256} /tmp/rclone.zip" | sha256sum -c - && \
    unzip /tmp/rclone.zip -d /tmp && \
    mv /tmp/rclone-*/rclone /usr/local/bin/rclone && \
    chmod +x /usr/local/bin/rclone && \
    rm -rf /tmp/rclone*

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
ENV PYTHONPATH=/app/backend

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
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8200", "--workers", "1", "--no-server-header"]
