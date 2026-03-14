"""Arkive CLI — arkive status, arkive backup --now, etc.

Exit codes:
    0 = success
    1 = failure
    2 = partial (some operations succeeded, some failed)
    3 = config error
    4 = auth error (401 Unauthorized)
    5 = backup already running (409 Conflict)
"""

import hashlib
import json
import sys

import click
import httpx

try:
    from app import __version__
except ImportError:
    import importlib.util
    import os

    _init = os.path.join(os.path.dirname(__file__), "__init__.py")
    _spec = importlib.util.spec_from_file_location("app", _init)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    __version__ = _mod.__version__

DEFAULT_URL = "http://localhost:8200"

# Exit codes
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_PARTIAL = 2
EXIT_CONFIG_ERROR = 3
EXIT_AUTH_ERROR = 4
EXIT_BACKUP_RUNNING = 5


def _get_client(api_key: str | None = None, base_url: str = DEFAULT_URL) -> httpx.Client:
    headers = {}
    if api_key:
        headers["X-API-Key"] = api_key
    return httpx.Client(base_url=base_url, headers=headers, timeout=300)


def _output(ctx, data: dict | list, human_fn=None):
    """Unified output: JSON mode prints data as JSON; otherwise calls human_fn.

    Respects --quiet flag to suppress non-error output.
    """
    if ctx.obj.get("quiet"):
        return
    if ctx.obj.get("json_mode"):
        click.echo(json.dumps(data, indent=2))
    elif human_fn:
        human_fn(data)


def _handle_error(e: Exception, ctx=None) -> None:
    """Print a user-friendly error message and exit with appropriate code.

    Exit codes: 1=failure, 4=auth error (401), 5=backup running (409).
    """
    json_mode = ctx.obj.get("json_mode") if ctx and ctx.obj else False
    exit_code = EXIT_FAILURE

    if isinstance(e, httpx.ConnectError):
        msg = "Cannot connect to Arkive server"
        if json_mode:
            click.echo(json.dumps({"error": msg}))
        else:
            click.echo(f"Error: {msg}", err=True)
    elif isinstance(e, httpx.HTTPStatusError):
        status_code = e.response.status_code
        try:
            body = e.response.json()
            msg = body.get("message") or body.get("detail") or body.get("error", str(e))
        except Exception:
            msg = e.response.text[:200] if e.response.text else str(e)
        if json_mode:
            click.echo(json.dumps({"error": msg, "status_code": status_code}))
        else:
            click.echo(f"Error: {status_code} — {msg}", err=True)
        # Map HTTP status to exit code
        if status_code == 401:
            exit_code = EXIT_AUTH_ERROR
        elif status_code == 409:
            exit_code = EXIT_BACKUP_RUNNING
    else:
        msg = str(e)
        if json_mode:
            click.echo(json.dumps({"error": msg}))
        else:
            click.echo(f"Error: {msg}", err=True)
    sys.exit(exit_code)


@click.group()
@click.option("--json", "json_mode", is_flag=True, help="Output as JSON")
@click.option("--verbose", "-v", is_flag=True, help="Debug-level output")
@click.option("--quiet", "-q", is_flag=True, help="Suppress non-error output")
@click.option("--api-key", envvar="ARKIVE_API_KEY", help="API key for authentication")
@click.option("--url", envvar="ARKIVE_URL", default=DEFAULT_URL, help="Arkive server URL")
@click.pass_context
def cli(ctx, json_mode, verbose, quiet, api_key, url):
    """Arkive — Automated disaster recovery for Unraid servers."""
    ctx.ensure_object(dict)
    ctx.obj["json_mode"] = json_mode
    ctx.obj["verbose"] = verbose
    ctx.obj["quiet"] = quiet
    ctx.obj["api_key"] = api_key
    ctx.obj["url"] = url


# ---------- version ----------


@cli.command()
@click.pass_context
def version(ctx):
    """Show Arkive version."""
    data = {"version": __version__}
    _output(ctx, data, lambda d: click.echo(f"Arkive v{d['version']}"))


# ---------- status ----------


@cli.command()
@click.pass_context
def status(ctx):
    """Show system status."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/status")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            click.echo(f"Arkive v{d['version']}")
            click.echo(f"Status: {d['status']}")
            click.echo(f"Platform: {d['platform']}")
            click.echo(f"Hostname: {d['hostname']}")
            click.echo(f"Uptime: {d['uptime_seconds']}s")
            click.echo(f"Setup: {'completed' if d['setup_completed'] else 'pending'}")
            targets = d.get("targets", {})
            click.echo(f"Targets: {targets.get('healthy', 0)}/{targets.get('total', 0)} healthy")
            if d.get("last_backup"):
                lb = d["last_backup"]
                click.echo(f"Last backup: {lb['status']} at {lb.get('started_at', 'unknown')}")

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


# ---------- backup ----------


@cli.group(invoke_without_command=True)
@click.option("--now", is_flag=True, help="Run backup immediately")
@click.option("--job-id", help="Specific job ID to run")
@click.pass_context
def backup(ctx, now, job_id):
    """Manage backups."""
    if now:
        _backup_run(ctx, job_id)
    elif ctx.invoked_subcommand is None:
        # Default: list jobs
        ctx.invoke(backup_list)


@backup.command("list")
@click.pass_context
def backup_list(ctx):
    """List backup jobs."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/jobs")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            jobs = d.get("items", d.get("jobs", []))
            if not jobs:
                click.echo("No backup jobs configured.")
                return
            for job in jobs:
                click.echo(f"  {job['id']}  {job['name']}  schedule={job['schedule']}  enabled={job['enabled']}")

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


def _backup_run(ctx, job_id):
    """Trigger a backup run."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        if not job_id:
            # Get first job
            r = client.get("/api/jobs")
            r.raise_for_status()
            resp = r.json()
            jobs = resp.get("items", resp.get("jobs", []))
            if not jobs:
                if ctx.obj.get("json_mode"):
                    click.echo(json.dumps({"error": "No backup jobs configured"}))
                else:
                    click.echo("No backup jobs configured", err=True)
                sys.exit(1)
            job_id = jobs[0]["id"]

        if not ctx.obj.get("json_mode"):
            click.echo(f"Triggering backup for job {job_id}...")

        r = client.post(f"/api/jobs/{job_id}/run")
        if r.status_code == 409:
            msg = "Another backup is already running"
            if ctx.obj.get("json_mode"):
                click.echo(json.dumps({"error": msg, "status_code": 409}))
            else:
                click.echo(f"Error: {msg}", err=True)
            sys.exit(EXIT_BACKUP_RUNNING)
        r.raise_for_status()
        data = r.json()

        def _human(d):
            click.echo(f"Backup {d.get('status', 'unknown')}: run_id={d.get('run_id', '')}")

        _output(ctx, data, _human)
    except SystemExit:
        raise
    except Exception as e:
        _handle_error(e, ctx)


# ---------- job ----------


@cli.group(invoke_without_command=True)
@click.pass_context
def job(ctx):
    """Manage backup jobs."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(job_list)


@job.command("list")
@click.pass_context
def job_list(ctx):
    """List all backup jobs with schedule and status."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/jobs")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            jobs = d.get("items", d.get("jobs", []))
            if not jobs:
                click.echo("No backup jobs configured.")
                return
            click.echo(f"  {'ID':<12} {'Name':<25} {'Schedule':<18} {'Enabled'}")
            click.echo(f"  {'─' * 12} {'─' * 25} {'─' * 18} {'─' * 8}")
            for j in jobs:
                enabled = click.style("yes", fg="green") if j.get("enabled") else click.style("no", fg="red")
                click.echo(f"  {j['id'][:11]:<12} {j.get('name', '?')[:24]:<25} {j.get('schedule', '?'):<18} {enabled}")
            click.echo(f"\n  Total: {len(jobs)} job(s)")

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


# ---------- targets ----------


@cli.group(invoke_without_command=True)
@click.pass_context
def targets(ctx):
    """Manage storage targets."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(targets_list)


@targets.command("list")
@click.pass_context
def targets_list(ctx):
    """List storage targets."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/targets")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            tgts = d.get("items", d.get("targets", []))
            if not tgts:
                click.echo("No storage targets configured.")
                return
            for t in tgts:
                click.echo(f"  {t['id']}  {t['name']}  type={t['type']}  status={t['status']}")

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


# ---------- snapshots ----------


@cli.group(invoke_without_command=True)
@click.pass_context
def snapshots(ctx):
    """Manage snapshots."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(snapshots_list)


@snapshots.command("list")
@click.pass_context
def snapshots_list(ctx):
    """List snapshots."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/snapshots")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            snaps = d.get("items", d.get("snapshots", []))
            if not snaps:
                click.echo("No snapshots found.")
                return
            for s in snaps:
                click.echo(
                    f"  {s['id']}  {s['time']}  target={s.get('target_id', 'N/A')}  size={s.get('size_bytes', 0)}"
                )

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


# ---------- discover ----------


@cli.group(invoke_without_command=True)
@click.pass_context
def discover(ctx):
    """Container discovery."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(discover_scan)


@discover.command("scan")
@click.pass_context
def discover_scan(ctx):
    """Run container discovery scan."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        if not ctx.obj.get("json_mode"):
            click.echo("Running discovery scan...")
        r = client.post("/api/discover/scan")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            click.echo(f"Found {d.get('total_containers', 0)} containers, {len(d.get('databases', []))} databases")
            click.echo(f"Scan took {d.get('scan_duration_seconds', 0)}s")

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


# ---------- databases ----------


@cli.command()
@click.pass_context
def databases(ctx):
    """List discovered databases across containers."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/databases")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            dbs = d.get("items", d.get("databases", []))
            if not dbs:
                click.echo("No databases discovered.")
                return
            # Table header
            click.echo(f"  {'Container':<25} {'DB Name':<20} {'Type':<12} {'Status'}")
            click.echo(f"  {'─' * 25} {'─' * 20} {'─' * 12} {'─' * 12}")
            for db in dbs:
                container = (db.get("container_name") or "?")[:24]
                db_name = (db.get("db_name") or "?")[:19]
                db_type = (db.get("db_type") or "?")[:11]
                status = db.get("status") or "discovered"
                click.echo(f"  {container:<25} {db_name:<20} {db_type:<12} {status}")
            click.echo(f"\n  Total: {len(dbs)} database(s)")

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


# ---------- health ----------


@cli.command()
@click.pass_context
def health(ctx):
    """Test connectivity to all storage targets."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/targets")
        r.raise_for_status()
        targets_data = r.json()
        tgts = targets_data.get("items", targets_data.get("targets", []))

        if not tgts:
            if ctx.obj.get("json_mode"):
                click.echo(json.dumps({"targets": [], "total": 0, "passed": 0, "failed": 0}))
            else:
                click.echo("No storage targets configured.")
            return

        results = []
        passed = 0
        failed = 0

        for t in tgts:
            target_id = t["id"]
            target_name = t.get("name", target_id)
            try:
                tr = client.post(f"/api/targets/{target_id}/test")
                tr.raise_for_status()
                result = tr.json()
                success = result.get("success", False)
            except Exception:
                success = False
                result = {"success": False, "message": "Connection test failed"}

            if success:
                passed += 1
            else:
                failed += 1

            results.append(
                {
                    "id": target_id,
                    "name": target_name,
                    "type": t.get("type", "unknown"),
                    "success": success,
                    "message": result.get("message", ""),
                }
            )

        data = {"targets": results, "total": len(results), "passed": passed, "failed": failed}

        def _human(d):
            for r in d["targets"]:
                mark = click.style("PASS", fg="green") if r["success"] else click.style("FAIL", fg="red")
                click.echo(f"  [{mark}] {r['name']} ({r['type']})")
                if r.get("message"):
                    click.echo(f"         {r['message']}")
            click.echo(f"\n  {d['passed']}/{d['total']} targets healthy")

        _output(ctx, data, _human)

        if failed > 0 and passed > 0:
            sys.exit(EXIT_PARTIAL)
        elif failed > 0:
            sys.exit(EXIT_FAILURE)
    except SystemExit:
        raise
    except Exception as e:
        _handle_error(e, ctx)


# ---------- logs ----------


@cli.command()
@click.option("--lines", "-n", default=50, type=int, help="Number of log lines to show")
@click.option("--level", default=None, help="Filter by log level (INFO, WARNING, ERROR)")
@click.pass_context
def logs(ctx, lines, level):
    """Show recent log entries."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        params = {"lines": lines}
        if level:
            params["level"] = level
        r = client.get("/api/logs", params=params)
        r.raise_for_status()
        data = r.json()

        def _human(d):
            entries = d.get("items", d.get("logs", []))
            if not entries:
                click.echo("No log entries found.")
                return
            for entry in entries:
                ts = entry.get("timestamp", "")[:19]
                lvl = entry.get("level", "INFO")
                msg = entry.get("message", "")
                component = entry.get("component", "")

                # Color-code the level
                level_colors = {
                    "ERROR": "red",
                    "WARNING": "yellow",
                    "WARN": "yellow",
                    "INFO": "blue",
                    "DEBUG": "white",
                }
                color = level_colors.get(lvl.upper(), "white")
                lvl_styled = click.style(f"{lvl:<7}", fg=color)

                prefix = f"  {ts}  {lvl_styled}"
                if component:
                    prefix += f"  [{component}]"
                click.echo(f"{prefix}  {msg}")

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


# ---------- notify ----------


@cli.command()
@click.option("--channel-id", required=True, help="Notification channel ID to test")
@click.pass_context
def notify(ctx, channel_id):
    """Send a test notification to a channel."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.post(f"/api/notifications/{channel_id}/test")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            success = d.get("success", d.get("status") == "ok")
            if success:
                click.echo(click.style("Test notification sent successfully.", fg="green"))
            else:
                msg = d.get("message", d.get("error", "Unknown error"))
                click.echo(click.style(f"Test notification failed: {msg}", fg="red"))

        _output(ctx, data, _human)

        # Exit with failure if the test itself reported failure
        success = data.get("success", data.get("status") == "ok")
        if not success:
            sys.exit(EXIT_FAILURE)
    except SystemExit:
        raise
    except Exception as e:
        _handle_error(e, ctx)


# ---------- key ----------


@cli.group()
@click.pass_context
def key(ctx):
    """API key management."""
    pass


@key.command("show-hash")
@click.pass_context
def key_show_hash(ctx):
    """Show the SHA-256 hash of the current API key."""
    api_key = ctx.obj.get("api_key")
    if not api_key:
        msg = "No API key provided. Use --api-key or set ARKIVE_API_KEY."
        if ctx.obj.get("json_mode"):
            click.echo(json.dumps({"error": msg}))
        else:
            click.echo(f"Error: {msg}", err=True)
        sys.exit(1)

    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    data = {"api_key_hash": key_hash, "api_key_prefix": api_key[:8] + "..."}

    def _human(d):
        click.echo(f"Key prefix: {d['api_key_prefix']}")
        click.echo(f"SHA-256:    {d['api_key_hash']}")

    _output(ctx, data, _human)


# ---------- restore ----------


@cli.group(invoke_without_command=True)
@click.pass_context
def restore(ctx):
    """Manage restores."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(restore_list)


@restore.command("list")
@click.pass_context
def restore_list(ctx):
    """List available snapshots for restore."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/snapshots")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            snaps = d.get("items", d.get("snapshots", []))
            if not snaps:
                click.echo("No snapshots available.")
                return
            for s in snaps:
                click.echo(
                    f"  {s['id']}  {s.get('time', 'N/A')}"
                    f"  target={s.get('target_id', 'N/A')}"
                    f"  size={s.get('size_bytes', 0)}"
                )

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


@restore.command("run")
@click.argument("snapshot_id")
@click.option("--target", required=True, help="Storage target ID to restore from")
@click.option("--paths", multiple=True, help="Specific paths to restore (repeatable)")
@click.option("--restore-to", required=True, help="Destination path for restored files")
@click.pass_context
def restore_run(ctx, snapshot_id, target, paths, restore_to):
    """Restore files from a snapshot."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        if not ctx.obj.get("json_mode"):
            click.echo(f"Restoring snapshot {snapshot_id}...")
        payload = {
            "snapshot_id": snapshot_id,
            "target": target,
            "paths": list(paths) if paths else ["/"],
        }
        if restore_to:
            payload["restore_to"] = restore_to
        r = client.post("/api/restore", json=payload)
        r.raise_for_status()
        data = r.json()

        def _human(d):
            status = d.get("status", "unknown")
            click.echo(f"Restore {status}: snapshot={snapshot_id}")
            if d.get("message"):
                click.echo(f"  {d['message']}")

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


# ---------- config ----------


@cli.group(invoke_without_command=True)
@click.pass_context
def config(ctx):
    """Manage configuration."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(config_show)


@config.command("show")
@click.pass_context
def config_show(ctx):
    """Show current settings."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/settings")
        r.raise_for_status()
        data = r.json()

        def _human(d):
            settings = d.get("settings", d) if isinstance(d, dict) else {}
            if isinstance(settings, dict):
                for key, value in sorted(settings.items()):
                    click.echo(f"  {key}: {value}")
            else:
                click.echo(json.dumps(d, indent=2))

        _output(ctx, data, _human)
    except Exception as e:
        _handle_error(e, ctx)


@config.command("export")
@click.pass_context
def config_export(ctx):
    """Export configuration as YAML."""
    client = _get_client(ctx.obj["api_key"], ctx.obj["url"])
    try:
        r = client.get("/api/settings/export")
        r.raise_for_status()
        # Export endpoint returns YAML content
        if ctx.obj.get("json_mode"):
            # Try to parse as JSON, fallback to wrapping raw text
            try:
                data = r.json()
                click.echo(json.dumps(data, indent=2))
            except Exception:
                click.echo(json.dumps({"yaml": r.text}))
        else:
            click.echo(r.text)
    except Exception as e:
        _handle_error(e, ctx)


def main():
    cli()


if __name__ == "__main__":
    main()
