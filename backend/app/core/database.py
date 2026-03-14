"""SQLite database initialization and schema management."""

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger("arkive.database")

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
INSERT OR IGNORE INTO schema_version (version) VALUES (1);

CREATE TABLE IF NOT EXISTS backup_jobs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'full',
    schedule TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    targets TEXT NOT NULL DEFAULT '[]',
    directories TEXT NOT NULL DEFAULT '[]',
    exclude_patterns TEXT NOT NULL DEFAULT '[]',
    include_databases INTEGER NOT NULL DEFAULT 1,
    include_flash INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS storage_targets (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'unknown',
    last_tested TEXT,
    snapshot_count INTEGER NOT NULL DEFAULT 0,
    total_size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS job_runs (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES backup_jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',
    trigger TEXT NOT NULL DEFAULT 'scheduled',
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    completed_at TEXT,
    duration_seconds INTEGER,
    databases_discovered INTEGER DEFAULT 0,
    databases_dumped INTEGER DEFAULT 0,
    databases_failed INTEGER DEFAULT 0,
    flash_backed_up INTEGER DEFAULT 0,
    flash_size_bytes INTEGER DEFAULT 0,
    total_size_bytes INTEGER DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_runs_job_id ON job_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_job_runs_started_at ON job_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS restore_runs (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    paths TEXT NOT NULL DEFAULT '[]',
    restore_to TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    completed_at TEXT,
    duration_seconds INTEGER,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_restore_runs_started_at ON restore_runs(started_at DESC);

CREATE TABLE IF NOT EXISTS job_run_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    snapshot_id TEXT,
    upload_bytes INTEGER DEFAULT 0,
    duration_seconds INTEGER DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_run_targets_run_id ON job_run_targets(run_id);

CREATE TABLE IF NOT EXISTS job_run_databases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES job_runs(id) ON DELETE CASCADE,
    container_name TEXT NOT NULL,
    db_type TEXT NOT NULL,
    db_name TEXT,
    dump_size_bytes INTEGER DEFAULT 0,
    integrity_check TEXT DEFAULT 'skipped',
    status TEXT NOT NULL DEFAULT 'pending',
    host_path TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_run_databases_run_id ON job_run_databases(run_id);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    full_id TEXT NOT NULL,
    time TEXT NOT NULL,
    hostname TEXT,
    paths TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    size_bytes INTEGER DEFAULT 0,
    cached_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (id, target_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_time ON snapshots(time DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_target_id ON snapshots(target_id);

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    action TEXT NOT NULL,
    message TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    severity TEXT NOT NULL DEFAULT 'info',
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_activity_log_timestamp ON activity_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_activity_log_type ON activity_log(type);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    encrypted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS notification_channels (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL DEFAULT '{}',
    events TEXT NOT NULL DEFAULT '[]',
    last_sent TEXT,
    last_status TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS discovered_containers (
    name TEXT PRIMARY KEY,
    image TEXT,
    status TEXT,
    ports TEXT NOT NULL DEFAULT '[]',
    mounts TEXT NOT NULL DEFAULT '[]',
    databases TEXT NOT NULL DEFAULT '[]',
    profile TEXT,
    priority TEXT DEFAULT 'medium',
    compose_project TEXT,
    last_scanned TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS watched_directories (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    exclude_patterns TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    size_bytes INTEGER,
    file_count INTEGER,
    last_scanned TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS size_history (
    date TEXT NOT NULL,
    target_id TEXT NOT NULL,
    total_size_bytes INTEGER NOT NULL,
    snapshot_count INTEGER NOT NULL,
    PRIMARY KEY (date, target_id)
);
"""


# Each entry is a list of SQL statements for that version.
# Version 1 is the baseline — already applied on first install via SCHEMA_SQL.
MIGRATIONS: dict[int, list[str]] = {
    # 1: baseline, applied by init_db
    2: [
        """CREATE TABLE IF NOT EXISTS restore_runs (
            id TEXT PRIMARY KEY,
            target_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            paths TEXT NOT NULL DEFAULT '[]',
            restore_to TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            completed_at TEXT,
            duration_seconds INTEGER,
            error_message TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_restore_runs_started_at ON restore_runs(started_at DESC)",
    ],
    3: [
        "CREATE INDEX IF NOT EXISTS idx_snapshots_target_id ON snapshots(target_id)",
        "CREATE INDEX IF NOT EXISTS idx_activity_log_type ON activity_log(type)",
    ],
}


async def run_migrations(db_path: str | Path) -> int:
    """Apply pending schema migrations. Returns number of migrations applied."""
    db_path = Path(db_path)
    async with aiosqlite.connect(db_path) as db:
        await db.execute("BEGIN EXCLUSIVE")
        try:
            cursor = await db.execute("SELECT MAX(version) FROM schema_version")
            row = await cursor.fetchone()
            current = row[0] if row and row[0] is not None else 0
        except Exception as e:
            logger.warning("Could not read schema_version (fresh DB?): %s", e)
            current = 0

        pending = sorted(v for v in MIGRATIONS if v > current)
        if not pending:
            await db.execute("COMMIT")
            return 0

        for version in pending:
            try:
                for sql in MIGRATIONS[version]:
                    await db.execute(sql)
                await db.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
            except Exception as exc:
                await db.rollback()
                logger.error("Migration %d failed: %s", version, exc)
                raise RuntimeError(f"Migration {version} failed: {exc}") from exc
        await db.execute("COMMIT")

        return len(pending)


async def init_db(db_path: str | Path) -> Path:
    """Initialize the database with schema and WAL mode."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(SCHEMA_SQL)
        await db.commit()
    logger.info("Database initialized at %s", db_path)
    return db_path


async def flush_wal(db_path: str | Path) -> None:
    """Flush WAL on shutdown."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    logger.info("WAL flushed for %s", db_path)
