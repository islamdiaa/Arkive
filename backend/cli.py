"""
Arkive CLI — Command-line interface for Arkive backup management.

Usage:
    arkive db init         - Initialize or migrate the database
    arkive db backup       - Create a database backup
    arkive db check        - Check database integrity
    arkive key generate    - Generate a new API key
    arkive key reset       - Reset the API key
    arkive key show-hash   - Show the hash of the current API key
    arkive job list        - List all backup jobs
    arkive job run <id>    - Trigger a backup job
    arkive job create      - Create a new backup job
    arkive restic init     - Initialize a restic repository
    arkive restic check    - Check restic repository integrity
    arkive restic unlock   - Unlock a restic repository
    arkive restic snapshots - List restic snapshots
    arkive restic stats    - Show restic repository stats
    arkive discovery scan  - Run container discovery
    arkive discovery list  - List discovered containers
    arkive version         - Show version info

All commands operate locally against the Arkive database and services.
Exit codes: 0 = success, 1 = failure, 2 = partial, 3 = config error,
            4 = not set up, 5 = connection refused (API server unreachable).
"""

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC
from pathlib import Path

import click

from app.core.config import ArkiveConfig
from app.core.database import init_db
from app.core.security import generate_api_key, hash_api_key

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_DIR = Path(os.environ.get("ARKIVE_CONFIG_DIR", "/config"))
DEFAULT_DB_PATH = DEFAULT_CONFIG_DIR / "arkive.db"
RESTIC_BIN = os.environ.get("RESTIC_BIN", "/usr/local/bin/restic")
VERSION = "0.1.0"

# Exit code constants
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_PARTIAL = 2
EXIT_CONFIG_ERROR = 3
EXIT_NOT_SETUP = 4  # Setup has not been completed
EXIT_CONNECTION_REFUSED = 5  # API server is unreachable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine from synchronous Click context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # If we're already in an async context, create a new event loop in a thread
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _check_mark(ok: bool) -> str:
    """Return a styled check mark or X for status display."""
    return click.style("OK", fg="green") if ok else click.style("FAIL", fg="red")


def _format_size(size_bytes: int) -> str:
    """Format bytes into human-readable size."""
    if size_bytes is None or size_bytes == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def _get_config() -> ArkiveConfig:
    """Load the Arkive configuration."""
    return ArkiveConfig()


def _get_db_path(db_path: str | None = None) -> Path:
    """Resolve the database path from argument or config."""
    if db_path:
        return Path(db_path)
    try:
        config = _get_config()
        return config.db_path
    except Exception:
        return DEFAULT_DB_PATH


def _get_db_connection(db_path: Path) -> sqlite3.Connection:
    """Open a synchronous SQLite connection."""
    if not db_path.exists():
        click.echo(click.style(f"Error: Database not found at {db_path}", fg="red"), err=True)
        sys.exit(EXIT_FAILURE)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _require_setup(db_path: Path) -> None:
    """Exit with code 4 (EXIT_NOT_SETUP) if setup has not been completed.

    Checks for the presence of api_key_hash in the settings table.
    Call this at the top of commands that require a configured Arkive instance.
    """
    if not db_path.exists():
        click.echo(
            click.style("Error: Arkive is not set up. Run the web setup wizard or 'arkive db init'.", fg="red"),
            err=True,
        )
        sys.exit(EXIT_NOT_SETUP)
    try:
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
        row = cursor.fetchone()
        conn.close()
        if not row:
            click.echo(
                click.style("Error: Arkive setup is not complete. Visit the web UI to finish setup.", fg="red"),
                err=True,
            )
            sys.exit(EXIT_NOT_SETUP)
    except sqlite3.Error as exc:
        click.echo(click.style(f"Error: Could not read setup state: {exc}", fg="red"), err=True)
        sys.exit(EXIT_CONFIG_ERROR)


def _check_api_connection(base_url: str = "http://localhost:8200") -> bool:
    """Return True if the Arkive API server is reachable.

    On failure, prints an error and exits with code 5 (EXIT_CONNECTION_REFUSED).
    """
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.urlopen(f"{base_url}/api/auth/session", timeout=3)
        req.read()
        return True
    except urllib.error.URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") else str(exc)
        click.echo(
            click.style(f"Error: Cannot reach Arkive API at {base_url}: {reason}", fg="red"),
            err=True,
        )
        sys.exit(EXIT_CONNECTION_REFUSED)
    except Exception as exc:
        click.echo(click.style(f"Error: Cannot reach Arkive API: {exc}", fg="red"), err=True)
        sys.exit(EXIT_CONNECTION_REFUSED)


def _read_api_key_file() -> str | None:
    """Read the raw API key from the config directory."""
    key_path = DEFAULT_CONFIG_DIR / ".api_key"
    if key_path.exists():
        return key_path.read_text().strip()
    return None


def _write_api_key_file(key: str) -> None:
    """Write the API key to the config directory with restricted permissions."""
    key_path = DEFAULT_CONFIG_DIR / ".api_key"
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(key)
    os.chmod(key_path, 0o600)


def _read_api_key_hash(db_path: Path) -> str | None:
    """Read the stored API key hash from the settings table."""
    try:
        conn = _get_db_connection(db_path)
        cursor = conn.execute("SELECT value FROM settings WHERE key = 'api_key_hash'")
        row = cursor.fetchone()
        conn.close()
        return row["value"] if row else None
    except Exception:
        return None


def _store_api_key_hash(db_path: Path, key_hash: str) -> None:
    """Store the API key hash in the settings table."""
    conn = _get_db_connection(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, encrypted, updated_at) "
        "VALUES ('api_key_hash', ?, 0, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
        (key_hash,),
    )
    conn.commit()
    conn.close()


def _build_restic_env(target_config: dict) -> dict:
    """Build environment variables for a restic command from target config."""
    env = os.environ.copy()
    repo = target_config.get("repository", target_config.get("path", ""))
    password = target_config.get("password", target_config.get("restic_password", ""))
    env["RESTIC_REPOSITORY"] = repo
    env["RESTIC_PASSWORD"] = password

    # S3 / B2 / cloud credentials
    for env_key in [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "B2_ACCOUNT_ID",
        "B2_ACCOUNT_KEY",
        "AZURE_ACCOUNT_NAME",
        "AZURE_ACCOUNT_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ]:
        val = target_config.get(env_key.lower(), target_config.get(env_key, ""))
        if val:
            env[env_key] = val

    return env


# ===========================================================================
# Root group
# ===========================================================================


@click.group()
@click.option("--json", "json_out", is_flag=True, help="Output as JSON", envvar="ARKIVE_JSON")
@click.pass_context
def cli(ctx, json_out: bool):
    """Arkive -- Automated disaster recovery for Unraid servers."""
    ctx.ensure_object(dict)
    ctx.obj["json"] = json_out


# ===========================================================================
# db — Database operations
# ===========================================================================


@cli.group()
@click.pass_context
def db(ctx):
    """Database operations (init, migrate, backup, check)."""
    pass


@db.command("init")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def db_init(ctx, db_path: str | None):
    """Initialize the database with the current schema.

    Creates the SQLite database with WAL mode enabled and all required tables.
    Safe to run on an already-initialized database (uses IF NOT EXISTS).
    """
    resolved = _get_db_path(db_path)
    click.echo(f"Initializing database at {resolved} ...")
    _run_async(init_db(resolved))
    click.echo(click.style("Database initialized successfully.", fg="green"))

    # Verify
    conn = sqlite3.connect(str(resolved))
    cursor = conn.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    cursor = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
    table_count = cursor.fetchone()[0]
    conn.close()

    click.echo(f"  Journal mode: {mode}")
    click.echo(f"  Tables:       {table_count}")
    sys.exit(0)


@db.command("migrate")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def db_migrate(ctx, db_path: str | None):
    """Run pending database migrations.

    Currently re-applies the full schema (safe with IF NOT EXISTS).
    Future versions will support incremental migrations.
    """
    resolved = _get_db_path(db_path)
    if not resolved.exists():
        click.echo(
            click.style(f"Error: Database not found at {resolved}. Run 'arkive db init' first.", fg="red"), err=True
        )
        sys.exit(EXIT_CONFIG_ERROR)

    click.echo(f"Running migrations on {resolved} ...")

    from app.core.database import run_migrations

    count = _run_async(run_migrations(resolved))
    if count:
        click.echo(click.style(f"Applied {count} migration(s).", fg="green"))
    else:
        click.echo(click.style("Schema already up to date.", fg="green"))
    sys.exit(0)


@db.command("backup")
@click.option("--db-path", default=None, help="Custom database path")
@click.option("--output", "-o", default=None, help="Output path for backup file")
@click.pass_context
def db_backup(ctx, db_path: str | None, output: str | None):
    """Create a backup copy of the database.

    Uses SQLite's online backup API to create a consistent copy
    even while the server is running.
    """
    resolved = _get_db_path(db_path)
    if not resolved.exists():
        click.echo(click.style(f"Error: Database not found at {resolved}", fg="red"), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    if output is None:
        from datetime import datetime

        timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        backup_dir = DEFAULT_CONFIG_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        output = str(backup_dir / f"arkive_{timestamp}.db")

    click.echo(f"Backing up {resolved} -> {output} ...")

    source = sqlite3.connect(str(resolved))
    dest = sqlite3.connect(output)
    source.backup(dest)
    dest.close()
    source.close()

    size = os.path.getsize(output)
    click.echo(click.style("Backup complete.", fg="green"))
    click.echo(f"  Output: {output}")
    click.echo(f"  Size:   {_format_size(size)}")
    sys.exit(0)


@db.command("check")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def db_check(ctx, db_path: str | None):
    """Check database integrity.

    Runs SQLite integrity_check and foreign_key_check PRAGMAs.
    Prints table counts and schema version.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)
    if not resolved.exists():
        click.echo(click.style(f"Error: Database not found at {resolved}", fg="red"), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row

    checks = {}

    # Integrity check
    cursor = conn.execute("PRAGMA integrity_check")
    integrity = cursor.fetchone()[0]
    checks["integrity"] = integrity

    # Foreign key check
    cursor = conn.execute("PRAGMA foreign_key_check")
    fk_errors = cursor.fetchall()
    checks["foreign_key_errors"] = len(fk_errors)

    # Journal mode
    cursor = conn.execute("PRAGMA journal_mode")
    checks["journal_mode"] = cursor.fetchone()[0]

    # Table count
    cursor = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
    checks["table_count"] = cursor.fetchone()[0]

    # Schema version
    try:
        cursor = conn.execute("SELECT MAX(version) FROM schema_version")
        checks["schema_version"] = cursor.fetchone()[0]
    except sqlite3.OperationalError:
        checks["schema_version"] = None

    # Row counts for key tables
    row_counts = {}
    for table in [
        "backup_jobs",
        "storage_targets",
        "job_runs",
        "snapshots",
        "discovered_containers",
        "activity_log",
        "settings",
    ]:
        try:
            cursor = conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            row_counts[table] = cursor.fetchone()[0]
        except sqlite3.OperationalError:
            row_counts[table] = None
    checks["row_counts"] = row_counts

    conn.close()

    ok = integrity == "ok" and checks["foreign_key_errors"] == 0

    if json_out:
        click.echo(json.dumps({"ok": ok, "checks": checks}, indent=2))
    else:
        click.echo("")
        click.echo(click.style("  Database Health Check", bold=True))
        click.echo(click.style("  =====================", fg="blue"))
        click.echo("")
        click.echo(f"  Path:           {resolved}")
        click.echo(f"  Integrity:      {_check_mark(integrity == 'ok')} ({integrity})")
        click.echo(
            f"  Foreign Keys:   {_check_mark(checks['foreign_key_errors'] == 0)} "
            f"({checks['foreign_key_errors']} errors)"
        )
        click.echo(f"  Journal Mode:   {checks['journal_mode']}")
        click.echo(f"  Schema Version: {checks['schema_version']}")
        click.echo(f"  Tables:         {checks['table_count']}")
        click.echo("")
        click.echo(click.style("  Row Counts:", bold=True))
        for table, count in row_counts.items():
            count_str = str(count) if count is not None else "N/A"
            click.echo(f"    {table:<25} {count_str}")
        click.echo("")

    sys.exit(0 if ok else 1)


# ===========================================================================
# key — API key management
# ===========================================================================


@cli.group()
@click.pass_context
def key(ctx):
    """API key management (generate, reset, show-hash)."""
    pass


@key.command("generate")
@click.option("--db-path", default=None, help="Custom database path")
@click.option("--force", is_flag=True, help="Overwrite existing key without prompting")
@click.pass_context
def key_generate(ctx, db_path: str | None, force: bool):
    """Generate a new API key.

    Creates a new ark_-prefixed API key, stores the hash in the database,
    and writes the plaintext key to /config/.api_key (mode 0600).
    The plaintext key is shown once — save it.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)

    # Check for existing key
    existing = _read_api_key_file()
    if existing and not force:
        click.echo(click.style("An API key already exists. Use --force to overwrite.", fg="yellow"), err=True)
        sys.exit(EXIT_FAILURE)

    new_key = generate_api_key()
    key_hash = hash_api_key(new_key)

    # Store hash in DB if database exists
    if resolved.exists():
        _store_api_key_hash(resolved, key_hash)

    # Write plaintext to file
    _write_api_key_file(new_key)

    if json_out:
        click.echo(json.dumps({"api_key": new_key, "hash": key_hash}))
    else:
        click.echo("")
        click.echo(click.style("  New API Key Generated", bold=True))
        click.echo(click.style("  =====================", fg="blue"))
        click.echo("")
        click.echo(f"  Key:  {click.style(new_key, fg='green', bold=True)}")
        click.echo(f"  Hash: {key_hash[:16]}...")
        click.echo(f"  File: {DEFAULT_CONFIG_DIR / '.api_key'}")
        click.echo("")
        click.echo(click.style("  Save this key — it will not be shown again.", fg="yellow"))
        click.echo("")

    sys.exit(0)


@key.command("reset")
@click.option("--db-path", default=None, help="Custom database path")
@click.confirmation_option(prompt="Are you sure you want to reset the API key?")
@click.pass_context
def key_reset(ctx, db_path: str | None):
    """Reset the API key.

    Generates a new key, invalidating the previous one immediately.
    Requires confirmation.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)

    new_key = generate_api_key()
    key_hash = hash_api_key(new_key)

    if resolved.exists():
        _store_api_key_hash(resolved, key_hash)

    _write_api_key_file(new_key)

    if json_out:
        click.echo(json.dumps({"api_key": new_key, "hash": key_hash}))
    else:
        click.echo("")
        click.echo(click.style("  API Key Reset", bold=True))
        click.echo("")
        click.echo(f"  New Key: {click.style(new_key, fg='green', bold=True)}")
        click.echo(f"  Hash:    {key_hash[:16]}...")
        click.echo("")
        click.echo(click.style("  The previous key has been invalidated.", fg="yellow"))
        click.echo("")

    sys.exit(0)


@key.command("show-hash")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def key_show_hash(ctx, db_path: str | None):
    """Show the hash of the current API key.

    Reads the stored hash from the database settings table.
    Also verifies consistency with the .api_key file if present.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)

    stored_hash = _read_api_key_hash(resolved) if resolved.exists() else None
    file_key = _read_api_key_file()

    file_hash = hash_api_key(file_key) if file_key else None
    consistent = stored_hash is not None and file_hash is not None and stored_hash == file_hash

    if json_out:
        click.echo(
            json.dumps(
                {
                    "stored_hash": stored_hash,
                    "file_exists": file_key is not None,
                    "consistent": consistent,
                }
            )
        )
    else:
        click.echo("")
        click.echo(click.style("  API Key Info", bold=True))
        click.echo("")
        if stored_hash:
            click.echo(f"  Stored Hash: {stored_hash}")
        else:
            click.echo(f"  Stored Hash: {click.style('None (not in database)', fg='yellow')}")

        click.echo(f"  Key File:    {'Present' if file_key else click.style('Missing', fg='yellow')}")
        if stored_hash and file_hash:
            mark = click.style("Consistent", fg="green") if consistent else click.style("MISMATCH", fg="red")
            click.echo(f"  Status:      {mark}")
        click.echo("")

    sys.exit(0)


# ===========================================================================
# job — Backup job operations
# ===========================================================================


@cli.group()
@click.pass_context
def job(ctx):
    """Backup job operations (list, run, create)."""
    pass


@job.command("list")
@click.option("--db-path", default=None, help="Custom database path")
@click.option("--enabled-only", is_flag=True, help="Show only enabled jobs")
@click.pass_context
def job_list(ctx, db_path: str | None, enabled_only: bool):
    """List all backup jobs.

    Shows job ID, name, schedule, enabled status, and last run info.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)
    _require_setup(resolved)
    conn = _get_db_connection(resolved)

    query = "SELECT * FROM backup_jobs"
    if enabled_only:
        query += " WHERE enabled = 1"
    query += " ORDER BY created_at DESC"

    cursor = conn.execute(query)
    rows = cursor.fetchall()

    jobs = []
    for row in rows:
        job_data = dict(row)
        # Get last run
        run_cursor = conn.execute(
            "SELECT status, started_at, duration_seconds FROM job_runs "
            "WHERE job_id = ? ORDER BY started_at DESC LIMIT 1",
            (row["id"],),
        )
        last_run = run_cursor.fetchone()
        if last_run:
            job_data["last_run_status"] = last_run["status"]
            job_data["last_run_at"] = last_run["started_at"]
            job_data["last_run_duration"] = last_run["duration_seconds"]
        else:
            job_data["last_run_status"] = None
            job_data["last_run_at"] = None
            job_data["last_run_duration"] = None
        jobs.append(job_data)

    conn.close()

    if json_out:
        click.echo(json.dumps({"items": jobs, "total": len(jobs)}, indent=2, default=str))
        sys.exit(0)

    click.echo("")
    click.echo(click.style("  Backup Jobs", bold=True))
    click.echo(click.style("  ===========", fg="blue"))
    click.echo("")

    if not jobs:
        click.echo("  No backup jobs found.")
        click.echo("")
        sys.exit(0)

    # Header
    click.echo(f"  {'ID':<10} {'Name':<20} {'Schedule':<15} {'Enabled':<9} {'Last Run':<12} {'Status'}")
    click.echo(f"  {'─' * 10} {'─' * 20} {'─' * 15} {'─' * 9} {'─' * 12} {'─' * 12}")

    for j in jobs:
        job_id = str(j["id"])[:8]
        name = (j["name"] or "")[:18]
        schedule = (j["schedule"] or "")[:13]
        enabled = click.style("Yes", fg="green") if j["enabled"] else click.style("No", fg="red")
        last_run = (j["last_run_at"] or "Never")[:10]
        last_status = j["last_run_status"] or "—"
        if last_status == "success":
            last_status = click.style("success", fg="green")
        elif last_status == "failed":
            last_status = click.style("failed", fg="red")
        elif last_status == "running":
            last_status = click.style("running", fg="yellow")

        click.echo(f"  {job_id:<10} {name:<20} {schedule:<15} {enabled:<18} {last_run:<12} {last_status}")

    click.echo("")
    click.echo(f"  Total: {len(jobs)} job(s)")
    click.echo("")
    sys.exit(0)


@job.command("run")
@click.argument("job_id")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def job_run(ctx, job_id: str, db_path: str | None):
    """Trigger a backup job by ID.

    Creates a new job run record with trigger='cli' and prints the run ID.
    Note: This creates the run record. The actual backup execution
    requires the API server to be running with the scheduler active.
    For full execution, use the API endpoint POST /api/jobs/{id}/run instead.
    """
    import uuid
    from datetime import datetime

    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)
    _require_setup(resolved)
    conn = _get_db_connection(resolved)

    # Verify job exists
    cursor = conn.execute("SELECT id, name, enabled FROM backup_jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    if not row:
        click.echo(click.style(f"Error: Job '{job_id}' not found.", fg="red"), err=True)
        conn.close()
        sys.exit(EXIT_FAILURE)

    if not row["enabled"]:
        click.echo(click.style(f"Warning: Job '{row['name']}' is disabled.", fg="yellow"), err=True)

    # Create a run record
    run_id = str(uuid.uuid4())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        "INSERT INTO job_runs (id, job_id, status, trigger, started_at) VALUES (?, ?, 'pending', 'cli', ?)",
        (run_id, job_id, now),
    )
    conn.commit()
    conn.close()

    if json_out:
        click.echo(json.dumps({"run_id": run_id, "job_id": job_id, "status": "pending", "trigger": "cli"}))
    else:
        click.echo("")
        click.echo(click.style("  Backup Job Triggered", fg="green", bold=True))
        click.echo("")
        click.echo(f"  Run ID:   {run_id}")
        click.echo(f"  Job ID:   {job_id}")
        click.echo(f"  Job Name: {row['name']}")
        click.echo("  Trigger:  CLI")
        click.echo("  Status:   pending")
        click.echo("")
        click.echo("  The backup scheduler will pick up this run.")
        click.echo("")

    sys.exit(0)


@job.command("create")
@click.option("--name", required=True, help="Job name")
@click.option("--schedule", required=True, help="Cron schedule expression (e.g. '0 2 * * *')")
@click.option("--type", "job_type", default="full", help="Job type (full, incremental)")
@click.option("--include-databases/--no-databases", default=True, help="Include database dumps")
@click.option("--include-flash/--no-flash", default=True, help="Include Unraid flash backup")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def job_create(
    ctx, name: str, schedule: str, job_type: str, include_databases: bool, include_flash: bool, db_path: str | None
):
    """Create a new backup job.

    Creates a job record in the database. The job will be picked up by
    the scheduler when the API server is running.
    """
    import uuid
    from datetime import datetime

    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)
    _require_setup(resolved)
    conn = _get_db_connection(resolved)

    job_id = str(uuid.uuid4())
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    conn.execute(
        "INSERT INTO backup_jobs (id, name, type, schedule, enabled, targets, directories, "
        "exclude_patterns, include_databases, include_flash, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, '[]', '[]', '[]', ?, ?, ?, ?)",
        (job_id, name, job_type, schedule, int(include_databases), int(include_flash), now, now),
    )
    conn.commit()
    conn.close()

    if json_out:
        click.echo(
            json.dumps(
                {
                    "id": job_id,
                    "name": name,
                    "type": job_type,
                    "schedule": schedule,
                    "include_databases": include_databases,
                    "include_flash": include_flash,
                }
            )
        )
    else:
        click.echo("")
        click.echo(click.style("  Backup Job Created", fg="green", bold=True))
        click.echo("")
        click.echo(f"  ID:        {job_id}")
        click.echo(f"  Name:      {name}")
        click.echo(f"  Type:      {job_type}")
        click.echo(f"  Schedule:  {schedule}")
        click.echo(f"  Databases: {'Yes' if include_databases else 'No'}")
        click.echo(f"  Flash:     {'Yes' if include_flash else 'No'}")
        click.echo("")

    sys.exit(0)


# ===========================================================================
# restic — Restic operations
# ===========================================================================


@cli.group()
@click.pass_context
def restic(ctx):
    """Restic operations (init, check, unlock, snapshots, stats)."""
    pass


def _get_target_env(target_id: str, db_path: Path) -> dict:
    """Load target config from DB and build restic environment variables."""
    conn = _get_db_connection(db_path)
    cursor = conn.execute("SELECT config FROM storage_targets WHERE id = ?", (target_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        click.echo(click.style(f"Error: Target '{target_id}' not found.", fg="red"), err=True)
        sys.exit(EXIT_CONFIG_ERROR)

    config_str = row["config"]

    # Try to decrypt config if encrypted
    try:
        from app.core.security import decrypt_config

        config = decrypt_config(config_str)
    except Exception:
        try:
            config = json.loads(config_str) if config_str else {}
        except (json.JSONDecodeError, TypeError):
            config = {}

    return _build_restic_env(config)


def _run_restic(args: list[str], env: dict, json_flag: bool = True) -> subprocess.CompletedProcess:
    """Run a restic command with optional --json flag."""
    cmd = [RESTIC_BIN] + args
    if json_flag:
        cmd.append("--json")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            env=env,
        )
        return result
    except FileNotFoundError:
        click.echo(click.style(f"Error: restic binary not found at {RESTIC_BIN}", fg="red"), err=True)
        sys.exit(EXIT_CONFIG_ERROR)
    except subprocess.TimeoutExpired:
        click.echo(click.style("Error: restic command timed out after 300s", fg="red"), err=True)
        sys.exit(EXIT_FAILURE)


@restic.command("init")
@click.option("--target-id", required=True, help="Storage target ID")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def restic_init(ctx, target_id: str, db_path: str | None):
    """Initialize a restic repository for a storage target.

    Creates the repository structure in the target's storage backend.
    """
    resolved = _get_db_path(db_path)
    env = _get_target_env(target_id, resolved)

    click.echo(f"Initializing restic repository for target {target_id} ...")
    result = _run_restic(["init"], env, json_flag=False)

    if result.returncode == 0:
        click.echo(click.style("Repository initialized successfully.", fg="green"))
        if result.stdout:
            click.echo(f"  {result.stdout.strip()}")
    else:
        # Already initialized is not an error
        if "already initialized" in (result.stderr or "").lower():
            click.echo(click.style("Repository already initialized.", fg="yellow"))
        else:
            click.echo(click.style(f"Error: {result.stderr.strip()}", fg="red"), err=True)
            sys.exit(EXIT_FAILURE)

    sys.exit(0)


@restic.command("check")
@click.option("--target-id", required=True, help="Storage target ID")
@click.option("--read-data", is_flag=True, help="Verify all data blobs (slow)")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def restic_check(ctx, target_id: str, read_data: bool, db_path: str | None):
    """Check restic repository integrity.

    Verifies the repository structure and optionally reads all data blobs.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)
    env = _get_target_env(target_id, resolved)

    args = ["check"]
    if read_data:
        args.append("--read-data")

    click.echo(f"Checking restic repository for target {target_id} ...")
    result = _run_restic(args, env, json_flag=False)

    if result.returncode == 0:
        if json_out:
            click.echo(json.dumps({"ok": True, "target_id": target_id, "output": result.stdout.strip()}))
        else:
            click.echo(click.style("Repository check passed.", fg="green"))
            if result.stdout:
                for line in result.stdout.strip().splitlines():
                    click.echo(f"  {line}")
    else:
        if json_out:
            click.echo(json.dumps({"ok": False, "target_id": target_id, "error": result.stderr.strip()}))
        else:
            click.echo(click.style("Repository check failed.", fg="red"), err=True)
            if result.stderr:
                for line in result.stderr.strip().splitlines():
                    click.echo(f"  {line}", err=True)
        sys.exit(EXIT_FAILURE)

    sys.exit(0)


@restic.command("unlock")
@click.option("--target-id", required=True, help="Storage target ID")
@click.option("--remove-all", is_flag=True, help="Remove all locks including non-stale")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def restic_unlock(ctx, target_id: str, remove_all: bool, db_path: str | None):
    """Unlock a restic repository.

    Removes stale locks. Use --remove-all to force-remove all locks.
    """
    resolved = _get_db_path(db_path)
    env = _get_target_env(target_id, resolved)

    args = ["unlock"]
    if remove_all:
        args.append("--remove-all")

    click.echo(f"Unlocking restic repository for target {target_id} ...")
    result = _run_restic(args, env, json_flag=False)

    if result.returncode == 0:
        click.echo(click.style("Repository unlocked successfully.", fg="green"))
    else:
        click.echo(click.style(f"Error: {result.stderr.strip()}", fg="red"), err=True)
        sys.exit(EXIT_FAILURE)

    sys.exit(0)


@restic.command("snapshots")
@click.option("--target-id", required=True, help="Storage target ID")
@click.option("--last", "last_n", default=0, type=int, help="Show only last N snapshots")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def restic_snapshots(ctx, target_id: str, last_n: int, db_path: str | None):
    """List snapshots in a restic repository.

    Queries the repository directly using restic --json for structured output.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)
    env = _get_target_env(target_id, resolved)

    args = ["snapshots"]
    if last_n > 0:
        args.extend(["--last", str(last_n)])

    result = _run_restic(args, env, json_flag=True)

    if result.returncode != 0:
        click.echo(click.style(f"Error listing snapshots: {result.stderr.strip()}", fg="red"), err=True)
        sys.exit(EXIT_FAILURE)

    try:
        snapshots_data = json.loads(result.stdout) if result.stdout else []
    except json.JSONDecodeError:
        click.echo(click.style("Error: Could not parse restic output.", fg="red"), err=True)
        sys.exit(EXIT_FAILURE)

    if json_out:
        click.echo(json.dumps({"target_id": target_id, "snapshots": snapshots_data}, indent=2))
        sys.exit(0)

    click.echo("")
    click.echo(click.style("  Restic Snapshots", bold=True))
    click.echo(click.style("  ================", fg="blue"))
    click.echo(f"  Target: {target_id}")
    click.echo("")

    if not snapshots_data:
        click.echo("  No snapshots found.")
        click.echo("")
        sys.exit(0)

    # Header
    click.echo(f"  {'Short ID':<12} {'Time':<22} {'Hostname':<15} {'Tags':<20} {'Paths'}")
    click.echo(f"  {'─' * 12} {'─' * 22} {'─' * 15} {'─' * 20} {'─' * 30}")

    for snap in snapshots_data:
        short_id = snap.get("short_id", snap.get("id", "?")[:8])
        time_str = snap.get("time", "?")[:19]
        hostname = (snap.get("hostname", "?") or "?")[:14]
        tags = ", ".join(snap.get("tags", [])[:3]) or "—"
        paths = ", ".join(snap.get("paths", [])[:2]) or "—"
        click.echo(f"  {short_id:<12} {time_str:<22} {hostname:<15} {tags:<20} {paths}")

    click.echo("")
    click.echo(f"  Total: {len(snapshots_data)} snapshot(s)")
    click.echo("")
    sys.exit(0)


@restic.command("stats")
@click.option("--target-id", required=True, help="Storage target ID")
@click.option(
    "--mode",
    default="restore-size",
    type=click.Choice(["restore-size", "files-by-contents", "raw-data"]),
    help="Stats mode",
)
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def restic_stats(ctx, target_id: str, mode: str, db_path: str | None):
    """Show restic repository statistics.

    Displays total size, file count, and other stats using restic --json.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)
    env = _get_target_env(target_id, resolved)

    args = ["stats", "--mode", mode]
    result = _run_restic(args, env, json_flag=True)

    if result.returncode != 0:
        click.echo(click.style(f"Error getting stats: {result.stderr.strip()}", fg="red"), err=True)
        sys.exit(EXIT_FAILURE)

    try:
        stats = json.loads(result.stdout) if result.stdout else {}
    except json.JSONDecodeError:
        click.echo(click.style("Error: Could not parse restic output.", fg="red"), err=True)
        sys.exit(EXIT_FAILURE)

    if json_out:
        click.echo(json.dumps({"target_id": target_id, "mode": mode, "stats": stats}, indent=2))
        sys.exit(0)

    click.echo("")
    click.echo(click.style("  Restic Repository Stats", bold=True))
    click.echo(click.style("  =======================", fg="blue"))
    click.echo(f"  Target: {target_id}")
    click.echo(f"  Mode:   {mode}")
    click.echo("")

    total_size = stats.get("total_size", 0)
    total_files = stats.get("total_file_count", stats.get("files_unmodified", 0))
    snapshots_count = stats.get("snapshots_count", 0)

    click.echo(f"  Total Size:     {_format_size(total_size)}")
    click.echo(f"  Total Files:    {total_files}")
    if snapshots_count:
        click.echo(f"  Snapshots:      {snapshots_count}")

    click.echo("")
    sys.exit(0)


# ===========================================================================
# discovery — Container discovery
# ===========================================================================


@cli.group()
@click.pass_context
def discovery(ctx):
    """Container discovery (scan, list)."""
    pass


@discovery.command("scan")
@click.option("--db-path", default=None, help="Custom database path")
@click.pass_context
def discovery_scan(ctx, db_path: str | None):
    """Run container discovery.

    Scans Docker containers for known database patterns using container
    profiles. Results are stored in the discovered_containers table.
    """
    json_out = ctx.obj.get("json", False)

    click.echo("Scanning Docker containers ...")

    try:
        import docker as docker_lib

        client = docker_lib.from_env()
        containers = client.containers.list(all=True)
    except Exception as e:
        click.echo(click.style(f"Error connecting to Docker: {e}", fg="red"), err=True)
        sys.exit(EXIT_CONNECTION_REFUSED)

    discovered = []
    databases_found = []

    for container in containers:
        info = {
            "name": container.name,
            "image": container.image.tags[0] if container.image.tags else str(container.image.id)[:12],
            "status": container.status,
            "ports": [],
            "mounts": [],
            "databases": [],
        }

        # Extract ports
        ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        for port_key, bindings in (ports or {}).items():
            if bindings:
                for b in bindings:
                    info["ports"].append(f"{b.get('HostIp', '')}:{b.get('HostPort', '')} -> {port_key}")
            else:
                info["ports"].append(port_key)

        # Extract mounts
        mounts = container.attrs.get("Mounts", [])
        for m in mounts:
            info["mounts"].append(
                {
                    "source": m.get("Source", ""),
                    "destination": m.get("Destination", ""),
                    "type": m.get("Type", ""),
                    "rw": m.get("RW", True),
                }
            )

        # Detect databases by image name patterns
        image_lower = info["image"].lower()
        db_patterns = {
            "postgres": "postgres",
            "mysql": "mysql",
            "mariadb": "mariadb",
            "mongo": "mongodb",
            "redis": "redis",
            "influxdb": "influxdb",
        }
        for pattern, db_type in db_patterns.items():
            if pattern in image_lower:
                db_entry = {
                    "container_name": container.name,
                    "db_type": db_type,
                    "db_name": container.name,
                }
                info["databases"].append(db_entry)
                databases_found.append(db_entry)

        # Check environment variables for database indicators
        env_vars = container.attrs.get("Config", {}).get("Env", [])
        env_db_hints = {
            "POSTGRES_DB": "postgres",
            "MYSQL_DATABASE": "mysql",
            "MARIADB_DATABASE": "mariadb",
            "MONGO_INITDB_DATABASE": "mongodb",
        }
        for env_var in env_vars:
            for hint_key, db_type in env_db_hints.items():
                if env_var.startswith(f"{hint_key}="):
                    db_name = env_var.split("=", 1)[1]
                    # Avoid duplicates
                    existing_types = {d["db_type"] for d in info["databases"]}
                    if db_type not in existing_types:
                        db_entry = {
                            "container_name": container.name,
                            "db_type": db_type,
                            "db_name": db_name,
                        }
                        info["databases"].append(db_entry)
                        databases_found.append(db_entry)

        discovered.append(info)

    # Store results in DB if available
    resolved = _get_db_path(db_path)
    if resolved.exists():
        try:
            conn = _get_db_connection(resolved)
            for c in discovered:
                conn.execute(
                    "INSERT OR REPLACE INTO discovered_containers "
                    "(name, image, status, ports, mounts, databases, last_scanned) "
                    "VALUES (?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
                    (
                        c["name"],
                        c["image"],
                        c["status"],
                        json.dumps(c["ports"]),
                        json.dumps(c["mounts"]),
                        json.dumps(c["databases"]),
                    ),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            click.echo(click.style(f"Warning: Could not store results in database: {e}", fg="yellow"), err=True)

    client.close()

    running = sum(1 for c in discovered if c["status"] == "running")
    stopped = len(discovered) - running

    if json_out:
        click.echo(
            json.dumps(
                {
                    "total_containers": len(discovered),
                    "running": running,
                    "stopped": stopped,
                    "databases_found": len(databases_found),
                    "containers": discovered,
                    "databases": databases_found,
                },
                indent=2,
            )
        )
    else:
        click.echo("")
        click.echo(click.style("  Discovery Results", bold=True))
        click.echo(click.style("  =================", fg="blue"))
        click.echo("")
        click.echo(f"  Containers Found: {len(discovered)} ({running} running, {stopped} stopped)")
        click.echo(f"  Databases Found:  {len(databases_found)}")
        click.echo("")

        if databases_found:
            click.echo(click.style("  Databases:", bold=True))
            for db in databases_found:
                click.echo(f"    {db['container_name']}/{db['db_name']} ({db['db_type']})")
            click.echo("")

        if discovered:
            click.echo(click.style("  Containers:", bold=True))
            for c in discovered:
                status_color = "green" if c["status"] == "running" else "yellow"
                db_indicator = f" [{len(c['databases'])} db]" if c["databases"] else ""
                click.echo(
                    f"    {c['name']:<30} {c['image']:<35} {click.style(c['status'], fg=status_color)}{db_indicator}"
                )
            click.echo("")

    sys.exit(0)


@discovery.command("list")
@click.option("--db-path", default=None, help="Custom database path")
@click.option("--with-databases", is_flag=True, help="Show only containers with databases")
@click.pass_context
def discovery_list(ctx, db_path: str | None, with_databases: bool):
    """List previously discovered containers.

    Shows containers from the last discovery scan stored in the database.
    """
    json_out = ctx.obj.get("json", False)
    resolved = _get_db_path(db_path)
    _require_setup(resolved)
    conn = _get_db_connection(resolved)

    query = "SELECT * FROM discovered_containers ORDER BY name"
    cursor = conn.execute(query)
    rows = cursor.fetchall()
    conn.close()

    items = []
    for row in rows:
        item = dict(row)
        # Parse JSON fields
        for field in ["ports", "mounts", "databases"]:
            try:
                item[field] = json.loads(item[field]) if item[field] else []
            except (json.JSONDecodeError, TypeError):
                item[field] = []
        items.append(item)

    if with_databases:
        items = [i for i in items if i["databases"]]

    if json_out:
        click.echo(json.dumps({"items": items, "total": len(items)}, indent=2, default=str))
        sys.exit(0)

    click.echo("")
    click.echo(click.style("  Discovered Containers", bold=True))
    click.echo(click.style("  =====================", fg="blue"))
    click.echo("")

    if not items:
        click.echo("  No discovered containers. Run 'arkive discovery scan' first.")
        click.echo("")
        sys.exit(0)

    # Header
    click.echo(f"  {'Name':<30} {'Image':<35} {'Status':<12} {'DBs':<5} {'Last Scanned'}")
    click.echo(f"  {'─' * 30} {'─' * 35} {'─' * 12} {'─' * 5} {'─' * 20}")

    for item in items:
        name = (item["name"] or "?")[:28]
        image = (item["image"] or "?")[:33]
        status = item.get("status", "?")
        status_color = "green" if status == "running" else "yellow"
        dbs = str(len(item["databases"]))
        last_scanned = (item.get("last_scanned") or "?")[:19]

        click.echo(f"  {name:<30} {image:<35} {click.style(status, fg=status_color):<21} {dbs:<5} {last_scanned}")

    click.echo("")
    click.echo(f"  Total: {len(items)} container(s)")
    click.echo("")
    sys.exit(0)


# ===========================================================================
# version — Show version info
# ===========================================================================


@cli.command("version")
@click.pass_context
def version(ctx):
    """Show Arkive version and system information."""
    import platform as platform_mod

    json_out = ctx.obj.get("json", False)

    info = {
        "arkive_version": VERSION,
        "python_version": platform_mod.python_version(),
        "platform": platform_mod.platform(),
        "architecture": platform_mod.machine(),
    }

    # Check restic version
    try:
        result = subprocess.run(
            [RESTIC_BIN, "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            info["restic_version"] = result.stdout.strip()
        else:
            info["restic_version"] = "not available"
    except Exception:
        info["restic_version"] = "not found"

    # Check Docker
    try:
        import docker as docker_lib

        client = docker_lib.from_env()
        docker_version = client.version()
        info["docker_version"] = docker_version.get("Version", "unknown")
        info["docker_api_version"] = docker_version.get("ApiVersion", "unknown")
        client.close()
    except Exception:
        info["docker_version"] = "not available"
        info["docker_api_version"] = "not available"

    # Platform detection
    try:
        from app.core.platform import detect_platform

        detected = detect_platform()
        info["detected_platform"] = detected.value
    except Exception:
        info["detected_platform"] = "unknown"

    # Config directory
    info["config_dir"] = str(DEFAULT_CONFIG_DIR)
    info["db_exists"] = DEFAULT_DB_PATH.exists()

    if json_out:
        click.echo(json.dumps(info, indent=2))
    else:
        click.echo("")
        click.echo(click.style("  Arkive", bold=True))
        click.echo(click.style("  ======", fg="blue"))
        click.echo("")
        click.echo(f"  Version:   {info['arkive_version']}")
        click.echo(f"  Platform:  {info['detected_platform']}")
        click.echo(f"  Python:    {info['python_version']}")
        click.echo(f"  Arch:      {info['architecture']}")
        click.echo(f"  Restic:    {info['restic_version']}")
        click.echo(f"  Docker:    {info['docker_version']}")
        click.echo(f"  Config:    {info['config_dir']}")
        click.echo(f"  Database:  {'Found' if info['db_exists'] else 'Not initialized'}")
        click.echo("")

    sys.exit(0)


# ===========================================================================
# Entry point
# ===========================================================================


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
