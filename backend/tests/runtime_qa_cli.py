#!/usr/bin/env python3
"""Arkive Runtime QA — CLI Integration Tests

Starts a real uvicorn server on port 8197, runs setup via httpx to obtain
an API key, then exercises every CLI command as a subprocess.

Run:  cd /home/ubuntu/arkive && python backend/tests/runtime_qa_cli.py

Environment:
  - No restic, rclone, or docker binaries expected
  - ARKIVE_CONFIG_DIR=/tmp/arkive-cli-qa (writable temp dir)
  - Server on port 8197

Exit code 0 = all pass, 1 = failures.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
import traceback

import httpx

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT = 8197
BASE_URL = f"http://127.0.0.1:{PORT}"
CONFIG_DIR = "/tmp/arkive-cli-qa2"
BACKEND_DIR = os.path.join(os.path.dirname(__file__), "..")
PROFILES_DIR = os.path.join(BACKEND_DIR, "..", "profiles")
STARTUP_TIMEOUT = 20  # seconds
SHUTDOWN_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []  # (name, passed, detail)
api_key: str = ""
server_proc: subprocess.Popen | None = None


def record(name: str, passed: bool, detail: str = ""):
    results.append((name, passed, detail))
    status = "\033[32mPASS\033[0m" if passed else "\033[31mFAIL\033[0m"
    line = f"  [{status}] {name}"
    if detail and not passed:
        line += f" -- {detail}"
    print(line)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------
def start_server() -> subprocess.Popen:
    """Start uvicorn server as subprocess."""
    if os.path.exists(CONFIG_DIR):
        shutil.rmtree(CONFIG_DIR)
    os.makedirs(os.path.join(CONFIG_DIR, "backups"), exist_ok=True)

    env = os.environ.copy()
    env["ARKIVE_CONFIG_DIR"] = CONFIG_DIR
    env["ARKIVE_LOG_LEVEL"] = "WARNING"
    env["ARKIVE_PROFILES_DIR"] = os.path.abspath(PROFILES_DIR)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=os.path.abspath(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def wait_for_server(timeout: float = STARTUP_TIMEOUT) -> bool:
    """Poll /api/status until 200 or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{BASE_URL}/api/status", timeout=2)
            if r.status_code == 200:
                return True
        except (httpx.ConnectError, httpx.ReadError):
            pass
        time.sleep(0.5)
    return False


def stop_server(proc: subprocess.Popen):
    """Graceful SIGTERM -> wait -> SIGKILL."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# CLI invocation helper
# ---------------------------------------------------------------------------
def run_cli(
    *args: str, api_key_arg: str | None = None, url: str | None = None, env_key: str | None = None, timeout: int = 30
) -> subprocess.CompletedProcess:
    """Run the CLI as a subprocess, return CompletedProcess."""
    cmd = [sys.executable, "-m", "app.cli"]
    if url:
        cmd.extend(["--url", url])
    if api_key_arg:
        cmd.extend(["--api-key", api_key_arg])
    cmd.extend(args)

    env = os.environ.copy()
    env["ARKIVE_CONFIG_DIR"] = CONFIG_DIR
    env["ARKIVE_PROFILES_DIR"] = os.path.abspath(PROFILES_DIR)
    if env_key:
        env["ARKIVE_API_KEY"] = env_key
    # Remove ARKIVE_URL so default doesn't interfere unless we set it
    env.pop("ARKIVE_URL", None)
    env.pop("ARKIVE_API_KEY", None)
    if env_key:
        env["ARKIVE_API_KEY"] = env_key

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=os.path.abspath(BACKEND_DIR),
        env=env,
    )
    return result


# ---------------------------------------------------------------------------
# HTTP helpers (for setup and target creation)
# ---------------------------------------------------------------------------
def api_get(path: str, auth: bool = True) -> httpx.Response:
    headers = {}
    if auth and api_key:
        headers["X-API-Key"] = api_key
    return httpx.get(f"{BASE_URL}{path}", headers=headers, timeout=10)


def api_post(path: str, body: dict | None = None, auth: bool = True) -> httpx.Response:
    headers = {}
    if auth and api_key:
        headers["X-API-Key"] = api_key
    return httpx.post(f"{BASE_URL}{path}", json=body, headers=headers, timeout=10)


def api_delete(path: str, auth: bool = True) -> httpx.Response:
    headers = {}
    if auth and api_key:
        headers["X-API-Key"] = api_key
    return httpx.delete(f"{BASE_URL}{path}", headers=headers, timeout=10)


# ===========================================================================
# TEST PHASES
# ===========================================================================


# ---------------------------------------------------------------------------
# Phase 1: Help & Version (4 tests)
# ---------------------------------------------------------------------------
def test_help_and_version():
    print("\n--- Phase 1: Help & Version ---")

    # 1. --help shows commands
    r = run_cli("--help", url=f"http://127.0.0.1:{PORT}")
    ok = r.returncode == 0 and "status" in r.stdout and "backup" in r.stdout
    record("--help shows commands (status, backup), exit 0", ok, f"rc={r.returncode}, stdout={r.stdout[:200]}")

    # 2. version command prints version
    r = run_cli("version", url=f"http://127.0.0.1:{PORT}")
    ok = r.returncode == 0 and "0.1.0" in (r.stdout + r.stderr)
    record("version prints 0.1.0, exit 0", ok, f"rc={r.returncode}, stdout={r.stdout.strip()}")

    # 3. status works (pre-setup, no auth needed)
    r = run_cli("status", url=f"http://127.0.0.1:{PORT}")
    ok = r.returncode == 0 and "Arkive" in r.stdout
    record("status (pre-setup) shows Arkive info, exit 0", ok, f"rc={r.returncode}, stdout={r.stdout[:200]}")

    # 4. nonexistent command exits non-zero or shows error
    r = run_cli("nonexistent-command", url=f"http://127.0.0.1:{PORT}")
    ok = r.returncode != 0 or "Error" in r.stderr or "No such command" in r.stderr or "Usage" in r.stderr
    record("nonexistent command exits non-zero or shows error", ok, f"rc={r.returncode}, stderr={r.stderr[:200]}")


# ---------------------------------------------------------------------------
# Phase 2: Status Command — Pre-Setup (4 tests)
# ---------------------------------------------------------------------------
def test_status_pre_setup():
    print("\n--- Phase 2: Status (Pre-Setup) ---")

    r = run_cli("status", url=f"http://127.0.0.1:{PORT}")
    out = r.stdout

    ok = r.returncode == 0 and "Arkive v0.1.0" in out
    record("status shows 'Arkive v0.1.0'", ok, f"rc={r.returncode}, stdout={out[:200]}")

    ok = "Setup: pending" in out
    record("status shows 'Setup: pending' before setup", ok, f"stdout={out[:200]}")

    ok = "Platform:" in out
    record("status shows 'Platform:'", ok, f"stdout={out[:200]}")

    ok = "Hostname:" in out
    record("status shows 'Hostname:'", ok, f"stdout={out[:200]}")


# ---------------------------------------------------------------------------
# Phase 3: Run Setup (obtain API key)
# ---------------------------------------------------------------------------
def do_setup():
    """Run setup via HTTP and return the API key."""
    global api_key
    r = api_post(
        "/api/auth/setup",
        {
            "encryption_password": "test-encryption-key-12345",
            "db_dump_schedule": "0 6,18 * * *",
            "cloud_sync_schedule": "0 7 * * *",
            "flash_schedule": "0 6 * * *",
            "directories": [],
            "run_first_backup": False,
        },
        auth=False,
    )
    d = r.json()
    if r.status_code == 200 and d.get("api_key", "").startswith("ark_"):
        api_key = d["api_key"]
        print(f"  Setup complete, API key: {api_key[:12]}...")
        return True
    print(f"  Setup FAILED: status={r.status_code}, body={r.text[:200]}")
    return False


# ---------------------------------------------------------------------------
# Phase 4: Status Command — Post-Setup (2 tests)
# ---------------------------------------------------------------------------
def test_status_post_setup():
    print("\n--- Phase 4: Status (Post-Setup) ---")

    r = run_cli("status", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    out = r.stdout

    ok = r.returncode == 0 and "Status: ok" in out
    record("status shows 'Status: ok', exit 0", ok, f"rc={r.returncode}, stdout={out[:200]}")

    ok = "Setup: completed" in out
    record("status shows 'Setup: completed' after setup", ok, f"stdout={out[:200]}")


# ---------------------------------------------------------------------------
# Phase 5: Backup Command (5 tests)
# ---------------------------------------------------------------------------
def test_backup_command():
    print("\n--- Phase 5: Backup Command ---")

    # 1. List jobs (no --now)
    r = run_cli("backup", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    out = r.stdout
    # After setup we should have 3 default jobs
    lines = [line for line in out.strip().splitlines() if line.strip()]
    ok = r.returncode == 0 and len(lines) >= 3
    record("backup (list) shows >= 3 jobs, exit 0", ok, f"rc={r.returncode}, lines={len(lines)}, stdout={out[:300]}")

    # 2. Check that job listing includes schedule info
    ok = "schedule=" in out
    record("backup list includes schedule= info", ok, f"stdout={out[:300]}")

    # 3. backup --now (triggers first job; will likely 500/503 due to no Docker, but CLI should not crash)
    r = run_cli("backup", "--now", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    combined = r.stdout + r.stderr
    # The CLI may exit 0 (if server returns a response) or 1 (if it gets a server error)
    # Key point: it should not crash with a Python traceback
    ok = "Traceback" not in combined
    record("backup --now does not crash (no traceback)", ok, f"rc={r.returncode}, combined={combined[:300]}")

    # 4. backup --now --job-id fake — handles error gracefully
    r = run_cli(
        "backup", "--now", "--job-id", "fake-nonexistent-id", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key
    )
    combined = r.stdout + r.stderr
    ok = "Traceback" not in combined
    record(
        "backup --now --job-id fake handles error (no traceback)", ok, f"rc={r.returncode}, combined={combined[:300]}"
    )

    # 5. backup --now --job-id fake exits non-zero or shows error text
    ok = r.returncode != 0 or "error" in combined.lower() or "Error" in combined
    record("backup --now --job-id fake signals error", ok, f"rc={r.returncode}, combined={combined[:200]}")


# ---------------------------------------------------------------------------
# Phase 6: Targets Command (3 tests)
# ---------------------------------------------------------------------------
def test_targets_command():
    print("\n--- Phase 6: Targets Command ---")

    # 1. Empty list
    r = run_cli("targets", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    ok = r.returncode == 0
    out = r.stdout.strip()
    # May be empty or show "No storage targets" message
    ok = ok and ("no" in out.lower() or "target" in out.lower() or len(out) == 0)
    record("targets shows empty list, exit 0", ok, f"rc={r.returncode}, stdout='{out[:200]}'")

    # 2. Create a target via API, then see it in CLI
    resp = api_post(
        "/api/targets",
        {
            "name": "CLI-QA-Target",
            "type": "local",
            "config": {"path": "/tmp/arkive-cli-qa/backups"},
        },
    )
    target_id = ""
    if resp.status_code == 201:
        target_id = resp.json().get("id", "")

    r = run_cli("targets", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    ok = r.returncode == 0 and "CLI-QA-Target" in r.stdout
    record("targets shows created target", ok, f"rc={r.returncode}, stdout={r.stdout[:200]}")

    # 3. Delete target via API, verify empty again
    if target_id:
        api_delete(f"/api/targets/{target_id}")
    r = run_cli("targets", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    out = r.stdout.strip()
    # May be empty or show "No storage targets" message, just not show the deleted target
    ok = r.returncode == 0 and "CLI-QA-Target" not in out
    record("targets back to empty after delete", ok, f"rc={r.returncode}, stdout='{out[:200]}'")


# ---------------------------------------------------------------------------
# Phase 7: Snapshots Command (2 tests)
# ---------------------------------------------------------------------------
def test_snapshots_command():
    print("\n--- Phase 7: Snapshots Command ---")

    r = run_cli("snapshots", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    ok = r.returncode == 0
    record("snapshots exits 0", ok, f"rc={r.returncode}")

    out = r.stdout.strip()
    # May be empty or show "No snapshots found" message
    ok = len(out) == 0 or "no snapshot" in out.lower()
    record("snapshots shows empty or message", ok, f"stdout='{out[:200]}'")


# ---------------------------------------------------------------------------
# Phase 8: Discovery Command (2 tests)
# ---------------------------------------------------------------------------
def test_discover_command():
    print("\n--- Phase 8: Discovery Command ---")

    r = run_cli("discover", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    combined = r.stdout + r.stderr
    # Without Docker, the server returns 503, CLI should handle this
    ok = r.returncode != 0 or "error" in combined.lower() or "Error" in combined or "unavailable" in combined.lower()
    record("discover fails gracefully (no Docker)", ok, f"rc={r.returncode}, combined={combined[:300]}")

    # Error message mentions Docker, unavailable, or connection issue
    ok = (
        "docker" in combined.lower()
        or "unavailable" in combined.lower()
        or "error" in combined.lower()
        or "503" in combined
    )
    record("discover error mentions Docker/unavailable/error", ok, f"combined={combined[:300]}")


# ---------------------------------------------------------------------------
# Phase 9: Auth (3 tests)
# ---------------------------------------------------------------------------
def test_auth():
    print("\n--- Phase 9: Auth ---")

    # 1. Without --api-key (post-setup): commands that need auth should fail
    r = run_cli("backup", url=f"http://127.0.0.1:{PORT}")
    combined = r.stdout + r.stderr
    ok = r.returncode != 0 or "error" in combined.lower() or "401" in combined
    record("backup without --api-key fails after setup", ok, f"rc={r.returncode}, combined={combined[:200]}")

    # 2. With --api-key: works
    r = run_cli("backup", url=f"http://127.0.0.1:{PORT}", api_key_arg=api_key)
    ok = r.returncode == 0
    record("backup with --api-key works", ok, f"rc={r.returncode}, stdout={r.stdout[:200]}")

    # 3. With ARKIVE_API_KEY env var: works
    r = run_cli("backup", url=f"http://127.0.0.1:{PORT}", env_key=api_key)
    ok = r.returncode == 0
    record("backup with ARKIVE_API_KEY env var works", ok, f"rc={r.returncode}, stdout={r.stdout[:200]}")


# ---------------------------------------------------------------------------
# Phase 10: Connection Errors (3 tests)
# ---------------------------------------------------------------------------
def test_connection_errors():
    print("\n--- Phase 10: Connection Errors ---")

    # 1. Wrong port
    r = run_cli("status", url="http://127.0.0.1:9999")
    combined = r.stdout + r.stderr
    ok = r.returncode != 0
    record("status with wrong port exits non-zero", ok, f"rc={r.returncode}")

    # 2. Error message mentions "Cannot connect" or similar
    ok = (
        "cannot connect" in combined.lower()
        or "connect" in combined.lower()
        or "error" in combined.lower()
        or "connection" in combined.lower()
    )
    record("connection error message is user-friendly", ok, f"combined={combined[:300]}")

    # 3. No Python traceback in output
    ok = "Traceback" not in combined
    record("connection error has no traceback", ok, f"combined={combined[:300]}")


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    global server_proc

    print("=" * 60)
    print("Arkive Runtime QA -- CLI Integration Tests")
    print("=" * 60)

    # Start server
    print(f"\nStarting server on port {PORT}...")
    server_proc = start_server()
    if not wait_for_server():
        stdout, stderr = b"", b""
        try:
            stdout = server_proc.stdout.read() if server_proc.stdout else b""
            stderr = server_proc.stderr.read() if server_proc.stderr else b""
        except Exception:
            pass
        print(f"\n\033[31mERROR: Server failed to start within {STARTUP_TIMEOUT}s\033[0m")
        print(f"stdout: {stdout.decode()[-1000:]}")
        print(f"stderr: {stderr.decode()[-1000:]}")
        stop_server(server_proc)
        sys.exit(1)
    print("Server ready\n")

    try:
        # Phase 1: Help & Version (before setup)
        try:
            test_help_and_version()
        except Exception as e:
            record(
                "test_help_and_version EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}"
            )

        # Phase 2: Status pre-setup
        try:
            test_status_pre_setup()
        except Exception as e:
            record(
                "test_status_pre_setup EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}"
            )

        # Phase 3: Run setup
        print("\n--- Phase 3: Setup ---")
        if not do_setup():
            record("Setup via API", False, "Could not complete setup")
            # Still try remaining tests
        else:
            record("Setup via API", True)

        # Phase 4: Status post-setup
        try:
            test_status_post_setup()
        except Exception as e:
            record(
                "test_status_post_setup EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}"
            )

        # Phase 5: Backup command
        try:
            test_backup_command()
        except Exception as e:
            record("test_backup_command EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")

        # Phase 6: Targets
        try:
            test_targets_command()
        except Exception as e:
            record("test_targets_command EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")

        # Phase 7: Snapshots
        try:
            test_snapshots_command()
        except Exception as e:
            record(
                "test_snapshots_command EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}"
            )

        # Phase 8: Discovery
        try:
            test_discover_command()
        except Exception as e:
            record(
                "test_discover_command EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}"
            )

        # Phase 9: Auth
        try:
            test_auth()
        except Exception as e:
            record("test_auth EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")

        # Phase 10: Connection errors
        try:
            test_connection_errors()
        except Exception as e:
            record(
                "test_connection_errors EXCEPTION", False, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}"
            )

    finally:
        print("\nStopping server...")
        stop_server(server_proc)

    # Summary
    total = len(results)
    passed = sum(1 for _, ok, _ in results if ok)
    failed = total - passed
    print("\n" + "=" * 60)
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f", \033[31m{failed} FAILED\033[0m")
        print("\nFailed tests:")
        for name, ok, detail in results:
            if not ok:
                print(f"  x {name}")
                if detail:
                    print(f"    {detail[:200]}")
    else:
        print(" -- \033[32mALL PASS\033[0m")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
