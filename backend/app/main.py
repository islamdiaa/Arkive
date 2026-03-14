"""Arkive — FastAPI application with 9-step startup sequence."""

import asyncio
import json
import logging
import os
import shutil
import socket
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app import __version__
from app.api.restore import cleanup_stale_restore_lock
from app.api.router import api_router
from app.core.config import ArkiveConfig
from app.core.database import flush_wal, init_db, run_migrations
from app.core.event_bus import EventBus
from app.core.exceptions import register_exception_handlers
from app.core.platform import Platform, detect_platform
from app.core.security import decrypt_config
from app.services.backup_engine import BackupEngine
from app.services.cloud_manager import CloudManager
from app.services.db_dumper import DBDumper
from app.services.discovery import DiscoveryEngine
from app.services.flash_backup import FlashBackup
from app.services.notifier import Notifier
from app.services.orchestrator import BackupOrchestrator, cleanup_stale_backup_lock
from app.services.restore_plan import RestorePlanGenerator
from app.services.scheduler import ArkiveScheduler
from app.utils.log_config import setup_logging

logger = logging.getLogger("arkive.main")

MAX_BODY_SIZE = 1_048_576  # 1MB


def _warn_unraid_runtime_permissions(platform: Platform) -> None:
    """Warn when Unraid is running without the host-read permissions Arkive expects."""
    if platform != Platform.UNRAID:
        return
    uid = os.geteuid()
    if uid == 0:
        return
    gid = os.getegid()
    logger.warning(
        "Unraid detected but Arkive is running as uid=%s gid=%s instead of root. "
        "Flash backup and some SQLite dumps may fail. Use --user 0:0 for full coverage.",
        uid,
        gid,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """9-step startup sequence."""
    # Step 1: Validate mounts
    docker_sock = Path("/var/run/docker.sock")
    config_dir = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config"))
    dev_mode = os.environ.get("ARKIVE_DEV_MODE", "0") == "1"

    if not docker_sock.exists():
        if dev_mode:
            logger.warning("Docker socket not found — running in limited mode (dev)")
        else:
            logger.critical("Docker socket not found at /var/run/docker.sock")
            raise SystemExit("Docker socket required. Mount with -v /var/run/docker.sock:/var/run/docker.sock:ro")

    config_dir.mkdir(parents=True, exist_ok=True)

    # Step 2: Validate binaries
    for binary in ["restic", "rclone"]:
        if not shutil.which(binary):
            if dev_mode:
                logger.warning("Binary not found: %s — limited mode (dev)", binary)
            else:
                logger.critical("Required binary not found: %s", binary)
                raise SystemExit(f"Required binary not found: {binary}. Install it or check your PATH.")

    # sqlite3 is optional (only used for integrity checks)
    if not shutil.which("sqlite3"):
        logger.info("sqlite3 binary not found — integrity checks will be skipped")

    # Step 3: Load config
    config = ArkiveConfig()
    config.ensure_dirs()
    setup_logging(config.log_dir, config.log_level)
    logger.info("Arkive v%s starting on %s", __version__, socket.gethostname())

    # Step 4: Initialize DB
    await init_db(config.db_path)
    await run_migrations(config.db_path)

    # Step 5: Detect platform
    platform = detect_platform()
    logger.info("Platform detected: %s", platform.value)
    _warn_unraid_runtime_permissions(platform)

    # Store platform in settings
    import aiosqlite

    async with aiosqlite.connect(config.db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('platform', ?)",
            (platform.value,),
        )
        await db.commit()

    # Step 6: First-run detection
    async with aiosqlite.connect(config.db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
        if not await cursor.fetchone():
            logger.info("Setup mode — serving setup wizard only")

    # Step 7: Self-healing — mark interrupted runs as failed
    async with aiosqlite.connect(config.db_path) as db:
        cursor = await db.execute("SELECT id FROM job_runs WHERE status = 'running'")
        stale_runs = await cursor.fetchall()
        if stale_runs:
            await db.execute(
                "UPDATE job_runs SET status = 'failed', error_message = 'Interrupted by server restart', "
                "completed_at = ? WHERE status = 'running'",
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
            )
            await db.commit()
            logger.warning("Cleaned %d stale job runs from previous shutdown", len(stale_runs))

        cursor = await db.execute("SELECT id FROM restore_runs WHERE status = 'running'")
        stale_restores = await cursor.fetchall()
        if stale_restores:
            await db.execute(
                "UPDATE restore_runs SET status = 'failed', error_message = 'Interrupted by server restart', "
                "completed_at = ? WHERE status = 'running'",
                (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
            )
            await db.execute(
                """INSERT INTO activity_log (type, action, message, details, severity)
                   VALUES ('restore', 'completed', ?, ?, 'warning')""",
                (
                    "Restore failed: Interrupted by server restart",
                    json.dumps({"stale_restore_runs": len(stale_restores), "status": "failed"}),
                ),
            )
            await db.commit()
            logger.warning("Cleaned %d stale restore runs from previous shutdown", len(stale_restores))

    if cleanup_stale_restore_lock(config.config_dir):
        logger.warning("Removed stale restore.lock from previous shutdown")
    if cleanup_stale_backup_lock(config.config_dir):
        logger.warning("Removed stale backup.lock from previous shutdown")

    # Initialize services
    event_bus = EventBus()

    # Docker client (optional — may not be available in dev)
    docker_client = None
    try:
        import docker

        docker_client = docker.from_env()
    except Exception as e:
        logger.warning("Docker client not available: %s", e)

    discovery = DiscoveryEngine(docker_client, config) if docker_client else None
    db_dumper = DBDumper(docker_client, config) if docker_client else None
    flash_backup = FlashBackup(config, platform)
    backup_engine = BackupEngine(config, docker_client=docker_client)
    cloud_manager = CloudManager(config)
    notifier = Notifier(config, event_bus)
    restore_plan = RestorePlanGenerator(config)

    orchestrator = BackupOrchestrator(
        discovery=discovery,
        db_dumper=db_dumper,
        flash_backup=flash_backup,
        backup_engine=backup_engine,
        cloud_manager=cloud_manager,
        notifier=notifier,
        event_bus=event_bus,
        config=config,
    )

    scheduler = ArkiveScheduler(
        orchestrator,
        config,
        discovery=discovery,
        backup_engine=backup_engine,
        cloud_manager=cloud_manager,
        notifier=notifier,
    )

    # Wire services to app.state
    app.state.config = config
    app.state.platform = platform
    app.state.event_bus = event_bus
    app.state.docker_client = docker_client
    app.state.discovery = discovery
    app.state.db_dumper = db_dumper
    app.state.flash_backup = flash_backup
    app.state.backup_engine = backup_engine
    app.state.cloud_manager = cloud_manager
    app.state.notifier = notifier
    app.state.restore_plan = restore_plan
    app.state.orchestrator = orchestrator
    app.state.scheduler = scheduler

    # Step 7b: Auto-unlock stale restic locks (part of self-healing, before scheduler)
    try:
        async with aiosqlite.connect(config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM storage_targets WHERE enabled = 1")
            targets = [dict(row) for row in await cursor.fetchall()]
        for t in targets:
            try:
                config_json = t.get("config", "{}")
                if config_json and config_json != "{}":
                    t["config"] = decrypt_config(config_json, str(config.config_dir))
                await backup_engine.unlock(t)
                logger.info("Auto-unlocked target: %s", t.get("name", t.get("id", "unknown")))
            except Exception as e:
                logger.warning("Auto-unlock failed for target %s: %s", t.get("name", ""), e)
    except Exception as e:
        logger.warning("Auto-unlock step failed: %s", e)

    # Step 8: Start scheduler
    if scheduler:
        await scheduler.start()

    # Step 9: Auto-discover on first boot
    if discovery:
        async with aiosqlite.connect(config.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT COUNT(*) as cnt FROM discovered_containers")
            count = (await cursor.fetchone())["cnt"]
            if count == 0:
                logger.info("First boot — running auto-discovery")
                try:
                    await discovery.scan()
                except Exception as e:
                    logger.warning("Auto-discovery failed: %s", e)

    logger.info("Arkive v%s ready", __version__)

    yield

    # Shutdown
    logger.info("Arkive shutting down...")

    # Send shutdown notification if notifications are configured
    try:
        await notifier.send(
            "system.shutdown",
            "Arkive Shutting Down",
            f"Arkive v{__version__} on {socket.gethostname()} is shutting down.",
            severity="info",
        )
    except Exception as e:
        logger.debug("Shutdown notification failed (non-critical): %s", e)

    # Wait for active backup to complete (up to 5 minutes, warn every 30s)
    if hasattr(orchestrator, "_active_runs") and orchestrator._active_runs:
        logger.info("Waiting for active backup to complete (max 5 min)...")
        waited = 0
        while waited < 300:
            if not (hasattr(orchestrator, "_active_runs") and orchestrator._active_runs):
                logger.info("Active backup completed during shutdown wait")
                break
            await asyncio.sleep(1)
            waited += 1
            if waited % 30 == 0:
                logger.warning(
                    "Still waiting for active backup to finish (%d/%d seconds)...",
                    waited,
                    300,
                )
        else:
            logger.warning("Backup still running after 5 min, forcing shutdown")

    if scheduler:
        await scheduler.stop()
    if docker_client:
        try:
            docker_client.close()
        except Exception:
            pass
    await flush_wal(config.db_path)
    logger.info("Shutdown complete")


class SPAStaticFiles(StaticFiles):
    """Static file handler that falls back to index.html for SPA routing.

    Skips paths that belong to the API or FastAPI docs so those routes
    are handled by FastAPI's router instead of the static file mount.
    """

    _PASSTHROUGH_EXACT = {"/api", "/docs", "/redoc", "/openapi.json"}
    _PASSTHROUGH_PREFIXES = ("/api/", "/docs/", "/redoc/")

    @classmethod
    def _should_passthrough(cls, path: str) -> bool:
        return path in cls._PASSTHROUGH_EXACT or any(path.startswith(p) for p in cls._PASSTHROUGH_PREFIXES)

    @staticmethod
    def _should_fallback_to_index(path: str, method: str) -> bool:
        if method not in {"GET", "HEAD"}:
            return False
        last_segment = path.rsplit("/", 1)[-1]
        return "." not in last_segment

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if self._should_passthrough(path):
                from starlette.responses import Response

                response = Response(status_code=404)
                await response(scope, receive, send)
                return
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope):
        request_path = scope.get("path", "")
        try:
            return await super().get_response(path, scope)
        except (FileNotFoundError, StarletteHTTPException) as exc:
            if isinstance(exc, StarletteHTTPException) and exc.status_code != 404:
                raise
            if self._should_passthrough(request_path) or not self._should_fallback_to_index(
                request_path, scope.get("method", "GET")
            ):
                raise
        return await super().get_response("index.html", scope)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Arkive",
        description="Automated disaster recovery for Unraid servers",
        version=__version__,
        lifespan=lifespan,
    )

    # CORS — single-user appliance; explicit origins required when credentials are involved
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://localhost:8200",
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8200",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Request body size limit — reject payloads over 1MB early
    @app.middleware("http")
    async def limit_request_body(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > MAX_BODY_SIZE:
                    return JSONResponse(
                        {"error": "payload_too_large", "message": "Request body too large", "details": {}},
                        status_code=413,
                    )
            except (ValueError, TypeError):
                return JSONResponse(
                    {"error": "bad_request", "message": "Invalid Content-Length header", "details": {}},
                    status_code=400,
                )
        return await call_next(request)

    # Security headers
    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # CSP: allow self for scripts/styles, block inline scripts except for SvelteKit
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    # Exception handlers
    register_exception_handlers(app)

    # API routes
    app.include_router(api_router)

    # Serve frontend static files
    frontend_dir = Path("/app/frontend/build")
    if frontend_dir.exists():
        app.mount("/", SPAStaticFiles(directory=str(frontend_dir), html=True), name="frontend")

    return app


class _ServerHeaderASGI:
    """ASGI wrapper that replaces the uvicorn 'server' header."""

    def __init__(self, inner):
        self._inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self._inner(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server"]
                headers.append((b"server", b"Arkive"))
                message = {**message, "headers": headers}
            await send(message)

        await self._inner(scope, receive, _send)


app = _ServerHeaderASGI(create_app())
