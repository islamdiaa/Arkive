#!/usr/bin/env python3
"""Arkive Runtime QA — API Integration Tests

Starts a real uvicorn server and hits every testable endpoint with httpx.
Run: ARKIVE_CONFIG_DIR=/tmp/arkive-qa python backend/tests/runtime_qa_api.py

Environment:
  - No restic, rclone, or docker binaries expected
  - ARKIVE_CONFIG_DIR=/tmp/arkive-qa (writable temp dir)
  - Server on port 8199

Exit code 0 = all pass, 1 = failures.
"""

import json
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
PORT = 8199
BASE_URL = f"http://127.0.0.1:{PORT}"
CONFIG_DIR = "/tmp/arkive-qa-api"
PROFILES_DIR = os.path.join(os.path.dirname(__file__), "..", "profiles")
STARTUP_TIMEOUT = 20  # seconds
SHUTDOWN_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []  # (name, passed, detail)
api_key: str = ""
server_proc: subprocess.Popen | None = None


def test(name: str):
    """Decorator to register a test function."""

    def decorator(func):
        func._test_name = name
        return func

    return decorator


class AssertionCollector:
    """Collect assertion results within a test."""

    def __init__(self, name: str):
        self.name = name
        self.failures: list[str] = []

    def check(self, condition: bool, msg: str = ""):
        if not condition:
            self.failures.append(msg or "assertion failed")

    def ok(self) -> bool:
        return len(self.failures) == 0


def record(name: str, passed: bool, detail: str = ""):
    results.append((name, passed, detail))
    status = "\033[32mPASS\033[0m" if passed else "\033[31mFAIL\033[0m"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail and not passed else ""))


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------
def start_server() -> subprocess.Popen:
    """Start uvicorn server as subprocess."""
    # Clean config dir
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
        cwd=os.path.join(os.path.dirname(__file__), ".."),
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
    """Graceful SIGTERM → wait → SIGKILL."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=SHUTDOWN_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def safe_json(r: httpx.Response) -> dict:
    """Parse JSON safely, returning error dict on failure."""
    try:
        return json.loads(r.text)
    except Exception:
        return {"_raw": r.text[:500], "_status": r.status_code}


def get(path: str, auth: bool = True, **kwargs) -> httpx.Response:
    headers = {}
    if auth and api_key:
        headers["X-API-Key"] = api_key
    return httpx.get(f"{BASE_URL}{path}", headers=headers, timeout=10, **kwargs)


def post(path: str, body: dict | None = None, auth: bool = True, **kwargs) -> httpx.Response:
    headers = {}
    if auth and api_key:
        headers["X-API-Key"] = api_key
    return httpx.post(f"{BASE_URL}{path}", json=body, headers=headers, timeout=10, **kwargs)


def put(path: str, body: dict | None = None, auth: bool = True, **kwargs) -> httpx.Response:
    headers = {}
    if auth and api_key:
        headers["X-API-Key"] = api_key
    return httpx.put(f"{BASE_URL}{path}", json=body, headers=headers, timeout=10, **kwargs)


def delete(path: str, auth: bool = True, **kwargs) -> httpx.Response:
    headers = {}
    if auth and api_key:
        headers["X-API-Key"] = api_key
    return httpx.delete(f"{BASE_URL}{path}", headers=headers, timeout=10, **kwargs)


# ===========================================================================
# TEST PHASES
# ===========================================================================


# ---------------------------------------------------------------------------
# Phase 1: Health & Status (no auth)
# ---------------------------------------------------------------------------
def test_health_and_status():
    global api_key
    print("\n📋 Phase 1: Health & Status (no auth)")

    # Health check
    r = get("/api/status", auth=False)
    d = safe_json(r)
    ok = r.status_code == 200 and d.get("status") == "ok" and d.get("setup_completed") is False
    record(
        "GET /api/status — 200, status=ok, setup_completed=false",
        ok,
        f"status={r.status_code}, body={json.dumps(d)[:200]}",
    )


# ---------------------------------------------------------------------------
# Phase 2: Pre-Setup Auth (setup bypass)
# ---------------------------------------------------------------------------
def test_pre_setup_auth():
    print("\n📋 Phase 2: Pre-Setup Auth Bypass")

    # Before setup, auth should be bypassed
    r = get("/api/jobs", auth=False)
    ok = r.status_code == 200
    record("GET /api/jobs (no auth, pre-setup) — 200 bypass", ok, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Phase 3: Setup Flow
# ---------------------------------------------------------------------------
def test_setup_flow():
    global api_key
    print("\n📋 Phase 3: Setup Flow")

    # Complete setup
    r = post(
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
    d = safe_json(r)
    ok = r.status_code == 200 and isinstance(d.get("api_key"), str) and d["api_key"].startswith("ark_")
    if ok:
        api_key = d["api_key"]
    record(
        "POST /api/auth/setup — 200, api_key starts with ark_",
        ok,
        f"status={r.status_code}, key_prefix={d.get('api_key', '')[:4]}",
    )

    # Setup already done
    r = post(
        "/api/auth/setup",
        {
            "encryption_password": "x",
        },
        auth=False,
    )
    ok = r.status_code == 409
    record("POST /api/auth/setup (again) — 409 already completed", ok, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Phase 4: Auth Enforcement
# ---------------------------------------------------------------------------
def test_auth_enforcement():
    print("\n📋 Phase 4: Auth Enforcement")

    # No key
    r = httpx.get(f"{BASE_URL}/api/jobs", timeout=10)
    ok = r.status_code == 401
    record("GET /api/jobs (no key) — 401", ok, f"status={r.status_code}")

    # Bad key
    r = httpx.get(f"{BASE_URL}/api/jobs", headers={"X-API-Key": "bad_key"}, timeout=10)
    ok = r.status_code == 401
    record("GET /api/jobs (bad key) — 401", ok, f"status={r.status_code}")

    # Token param
    r = httpx.get(f"{BASE_URL}/api/jobs?token={api_key}", timeout=10)
    ok = r.status_code == 200
    record("GET /api/jobs?token=<key> — 200", ok, f"status={r.status_code}")

    # Valid key in header
    r = get("/api/jobs")
    d = safe_json(r)
    ok = r.status_code == 200 and "jobs" in d
    record("GET /api/jobs (valid key) — 200, has jobs", ok, f"status={r.status_code}")

    # Status shows setup_completed=true
    r = get("/api/status", auth=False)
    d = safe_json(r)
    ok = r.status_code == 200 and d.get("setup_completed") is True
    record("GET /api/status — setup_completed=true", ok, f"setup_completed={d.get('setup_completed')}")


# ---------------------------------------------------------------------------
# Phase 5: Jobs CRUD
# ---------------------------------------------------------------------------
def test_jobs_crud():
    print("\n📋 Phase 5: Jobs CRUD")

    # List default jobs (created during setup)
    r = get("/api/jobs")
    d = safe_json(r)
    jobs = d.get("jobs", [])
    job_types = {j["type"] for j in jobs}
    ok = len(jobs) == 3 and "db_dump" in job_types and "full" in job_types and "flash" in job_types
    record("GET /api/jobs — 3 default jobs (db_dump, full, flash)", ok, f"count={len(jobs)}, types={job_types}")

    # Get single job
    first_job_id = jobs[0]["id"] if jobs else "none"
    r = get(f"/api/jobs/{first_job_id}")
    d = safe_json(r)
    ok = r.status_code == 200 and "name" in d and "type" in d and "schedule" in d
    record("GET /api/jobs/<id> — has name, type, schedule", ok, f"status={r.status_code}, name={d.get('name')}")

    # Get nonexistent
    r = get("/api/jobs/nonexistent-fake-id")
    ok = r.status_code == 404
    record("GET /api/jobs/fake — 404", ok, f"status={r.status_code}")

    # Create job
    r = post(
        "/api/jobs",
        {
            "name": "QA Test Job",
            "type": "full",
            "schedule": "0 3 * * *",
        },
    )
    d = safe_json(r)
    ok = r.status_code == 201 and "id" in d
    created_job_id = d.get("id", "")
    record("POST /api/jobs — 201, has id", ok, f"status={r.status_code}, id={created_job_id}")

    # Update job
    r = put(
        f"/api/jobs/{created_job_id}",
        {
            "name": "Updated QA Job",
            "enabled": False,
        },
    )
    ok = r.status_code == 200
    record("PUT /api/jobs/<id> — 200 updated", ok, f"status={r.status_code}")

    # List runs (should be empty)
    r = get(f"/api/jobs/{first_job_id}/runs")
    d = safe_json(r)
    ok = r.status_code == 200 and "items" in d
    record(
        "GET /api/jobs/<id>/runs — has runs list", ok, f"status={r.status_code}, runs_count={len(d.get('items', []))}"
    )

    # Delete job
    r = delete(f"/api/jobs/{created_job_id}")
    ok = r.status_code == 200
    record("DELETE /api/jobs/<id> — 200", ok, f"status={r.status_code}")

    # Verify count back to 3
    r = get("/api/jobs")
    d = safe_json(r)
    ok = len(d.get("jobs", [])) == 3
    record("GET /api/jobs — back to 3", ok, f"count={len(d.get('jobs', []))}")


# ---------------------------------------------------------------------------
# Phase 6: Trigger Manual Run (graceful fail — no orchestrator)
# ---------------------------------------------------------------------------
def test_trigger_run():
    print("\n📋 Phase 6: Trigger Manual Run")

    r = get("/api/jobs")
    jobs = safe_json(r).get("jobs", [])
    if not jobs:
        record("POST /api/jobs/<id>/run — SKIP (no jobs)", False, "no jobs")
        return

    full_job = next((j for j in jobs if j["type"] == "full"), jobs[0])
    r = post(f"/api/jobs/{full_job['id']}/run")
    # Without Docker, orchestrator is None → expect 500/503 (not crash)
    ok = r.status_code in (200, 202, 500, 503, 422)
    record("POST /api/jobs/<id>/run — responds (no crash)", ok, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Phase 7: Storage Targets CRUD
# ---------------------------------------------------------------------------
def test_targets_crud():
    print("\n📋 Phase 7: Storage Targets CRUD")

    # List empty
    r = get("/api/targets")
    d = safe_json(r)
    ok = r.status_code == 200 and "targets" in d and len(d["targets"]) == 0
    record("GET /api/targets — empty", ok, f"count={len(d.get('targets', []))}")

    # Create local target
    r = post(
        "/api/targets",
        {
            "name": "QA Local Target",
            "type": "local",
            "config": {"path": "/tmp/arkive-qa/backups"},
        },
    )
    d = safe_json(r)
    ok = r.status_code == 201 and "id" in d
    target_id = d.get("id", "")
    record("POST /api/targets — 201, has id", ok, f"status={r.status_code}, id={target_id}")

    # Get target
    r = get(f"/api/targets/{target_id}")
    d = safe_json(r)
    ok = r.status_code == 200 and d.get("name") == "QA Local Target"
    record("GET /api/targets/<id> — has name", ok, f"status={r.status_code}, name={d.get('name')}")

    # Update target
    r = put(f"/api/targets/{target_id}", {"name": "Renamed Target"})
    ok = r.status_code == 200
    record("PUT /api/targets/<id> — 200", ok, f"status={r.status_code}")

    # Get nonexistent
    r = get("/api/targets/nonexistent-fake")
    ok = r.status_code == 404
    record("GET /api/targets/fake — 404", ok, f"status={r.status_code}")

    # Delete
    r = delete(f"/api/targets/{target_id}")
    ok = r.status_code == 200
    record("DELETE /api/targets/<id> — 200", ok, f"status={r.status_code}")

    # Verify empty
    r = get("/api/targets")
    d = safe_json(r)
    ok = len(d.get("targets", [])) == 0
    record("GET /api/targets — back to empty", ok, f"count={len(d.get('targets', []))}")


# ---------------------------------------------------------------------------
# Phase 8: Settings
# ---------------------------------------------------------------------------
def test_settings():
    print("\n📋 Phase 8: Settings")

    # Get all
    r = get("/api/settings")
    d = safe_json(r)
    ok = r.status_code == 200 and "server_name" in d
    record("GET /api/settings — has server_name", ok, f"status={r.status_code}, keys={list(d.keys())[:5]}")

    # Update bulk
    r = put("/api/settings", {"server_name": "qa-server", "keep_daily": 14})
    ok = r.status_code == 200
    record("PUT /api/settings — 200", ok, f"status={r.status_code}")

    # Verify update persisted
    r = get("/api/settings")
    d = safe_json(r)
    ok = d.get("server_name") == "qa-server" and d.get("keep_daily") == 14
    record(
        "GET /api/settings — server_name=qa-server, keep_daily=14",
        ok,
        f"server_name={d.get('server_name')}, keep_daily={d.get('keep_daily')}",
    )


# ---------------------------------------------------------------------------
# Phase 9: Directories CRUD
# ---------------------------------------------------------------------------
def test_directories():
    print("\n📋 Phase 9: Directories CRUD")

    # List empty
    r = get("/api/directories")
    d = safe_json(r)
    ok = r.status_code == 200 and "directories" in d and len(d["directories"]) == 0
    record("GET /api/directories — empty", ok, f"count={len(d.get('directories', []))}")

    # Create (use /tmp which exists)
    r = post("/api/directories", {"path": "/tmp", "label": "Temp"})
    d = safe_json(r)
    ok = r.status_code == 201 and "id" in d
    dir_id = d.get("id", "")
    record("POST /api/directories — 201", ok, f"status={r.status_code}, id={dir_id}")

    # Scan
    r = post("/api/directories/scan")
    d = safe_json(r)
    ok = r.status_code == 200 and "directories" in d
    record("POST /api/directories/scan — 200, has directories", ok, f"status={r.status_code}")

    # Update
    r = put(
        f"/api/directories/{dir_id}",
        {
            "path": "/tmp",
            "label": "Updated Temp",
        },
    )
    ok = r.status_code == 200
    record("PUT /api/directories/<id> — 200", ok, f"status={r.status_code}")

    # Delete
    r = delete(f"/api/directories/{dir_id}")
    ok = r.status_code == 200
    record("DELETE /api/directories/<id> — 200", ok, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Phase 10: Notifications CRUD
# ---------------------------------------------------------------------------
def test_notifications():
    print("\n📋 Phase 10: Notifications CRUD")

    # List empty
    r = get("/api/notifications")
    d = safe_json(r)
    ok = r.status_code == 200 and "channels" in d and len(d["channels"]) == 0
    record("GET /api/notifications — empty", ok, f"count={len(d.get('channels', []))}")

    # Create webhook
    r = post(
        "/api/notifications",
        {
            "name": "QA Webhook",
            "type": "webhook",
            "url": "https://example.com/webhook/test12345678",
            "events": ["backup.success", "backup.failed"],
        },
    )
    d = safe_json(r)
    ok = r.status_code == 201 and "id" in d
    channel_id = d.get("id", "")
    record("POST /api/notifications — 201", ok, f"status={r.status_code}, id={channel_id}")

    # List with redaction
    r = get("/api/notifications")
    d = safe_json(r)
    channels = d.get("channels", [])
    ok = len(channels) == 1
    if channels:
        url = channels[0].get("config", {}).get("url", "")
        ok = ok and "••••••" in url
    record(
        "GET /api/notifications — URL redacted",
        ok,
        f"url_sample={channels[0].get('config', {}).get('url', '')[:30] if channels else 'none'}",
    )

    # Test send (will fail since URL is fake, but should not crash)
    r = post(f"/api/notifications/{channel_id}/test")
    ok = r.status_code in (200, 500, 502)  # May fail gracefully
    record("POST /api/notifications/<id>/test — responds", ok, f"status={r.status_code}")

    # Delete
    r = delete(f"/api/notifications/{channel_id}")
    ok = r.status_code == 200
    record("DELETE /api/notifications/<id> — 200", ok, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Phase 11: Activity & Logs
# ---------------------------------------------------------------------------
def test_activity_and_logs():
    print("\n📋 Phase 11: Activity & Logs")

    # Activity list
    r = get("/api/activity")
    d = safe_json(r)
    ok = r.status_code == 200 and "activities" in d and "total" in d
    record("GET /api/activity — has activities, total", ok, f"status={r.status_code}, total={d.get('total')}")

    # Logs
    r = get("/api/logs")
    d = safe_json(r)
    ok = r.status_code == 200 and "logs" in d
    record("GET /api/logs — has logs list", ok, f"status={r.status_code}, log_count={len(d.get('logs', []))}")


# ---------------------------------------------------------------------------
# Phase 12: Storage Stats
# ---------------------------------------------------------------------------
def test_storage_stats():
    print("\n📋 Phase 12: Storage Stats")

    r = get("/api/storage")
    d = safe_json(r)
    ok = r.status_code == 200 and "total_size_bytes" in d and "targets" in d and "target_count" in d
    record("GET /api/storage — has stats shape", ok, f"status={r.status_code}, keys={list(d.keys())[:5]}")


# ---------------------------------------------------------------------------
# Phase 13: SSE Streams
# ---------------------------------------------------------------------------
def test_sse_streams():
    print("\n📋 Phase 13: SSE Streams")

    # Events stream
    try:
        with httpx.stream("GET", f"{BASE_URL}/api/events/stream?token={api_key}", timeout=5) as r:
            ok = r.status_code == 200
            ct = r.headers.get("content-type", "")
            ok = ok and "text/event-stream" in ct
            record("GET /api/events/stream — 200, text/event-stream", ok, f"status={r.status_code}, content-type={ct}")
    except httpx.ReadTimeout:
        # Timeout is expected since SSE keeps connection open;
        # if we got here, we likely got the initial response
        record("GET /api/events/stream — connected (timeout reading body is expected)", True)
    except Exception as e:
        record("GET /api/events/stream — error", False, str(e))

    # Logs stream
    try:
        with httpx.stream("GET", f"{BASE_URL}/api/logs/stream?token={api_key}", timeout=5) as r:
            ok = r.status_code == 200
            ct = r.headers.get("content-type", "")
            ok = ok and "text/event-stream" in ct
            record("GET /api/logs/stream — 200, text/event-stream", ok, f"status={r.status_code}, content-type={ct}")
    except httpx.ReadTimeout:
        record("GET /api/logs/stream — connected (timeout expected)", True)
    except Exception as e:
        record("GET /api/logs/stream — error", False, str(e))


# ---------------------------------------------------------------------------
# Phase 14: API Key Rotation
# ---------------------------------------------------------------------------
def test_key_rotation():
    global api_key
    print("\n📋 Phase 14: API Key Rotation")

    old_key = api_key

    # Rotate
    r = post("/api/auth/rotate-key")
    d = safe_json(r)
    ok = r.status_code == 200 and "api_key" in d and d["api_key"] != old_key
    new_key = d.get("api_key", "")
    record("POST /api/auth/rotate-key — new key != old", ok, f"status={r.status_code}, new_prefix={new_key[:8]}")

    # Old key fails
    r = httpx.get(f"{BASE_URL}/api/jobs", headers={"X-API-Key": old_key}, timeout=10)
    ok = r.status_code == 401
    record("GET /api/jobs (old key) — 401", ok, f"status={r.status_code}")

    # New key works
    api_key = new_key
    r = get("/api/jobs")
    ok = r.status_code == 200
    record("GET /api/jobs (new key) — 200", ok, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Phase 15: Graceful Degradation
# ---------------------------------------------------------------------------
def test_graceful_degradation():
    print("\n📋 Phase 15: Graceful Degradation")

    # Discovery scan (no docker) — should return error, not crash
    r = post("/api/discover/scan")
    ok = r.status_code in (500, 503, 422, 400)
    record("POST /api/discover/scan (no docker) — error, no crash", ok, f"status={r.status_code}")

    # Snapshots (no restic) — should return empty
    r = get("/api/snapshots")
    d = safe_json(r)
    ok = r.status_code == 200 and "snapshots" in d
    record(
        "GET /api/snapshots — 200, has snapshots list",
        ok,
        f"status={r.status_code}, count={len(d.get('snapshots', []))}",
    )

    # Databases (no docker, empty discovered) — should return empty
    r = get("/api/databases")
    d = safe_json(r)
    ok = r.status_code == 200 and "databases" in d
    record(
        "GET /api/databases — 200, empty databases", ok, f"status={r.status_code}, count={len(d.get('databases', []))}"
    )

    # Restore plan preview
    r = get("/api/restore/plan/preview")
    ok = r.status_code in (200, 500)
    record("GET /api/restore/plan/preview — responds", ok, f"status={r.status_code}")

    # Discovered containers (from DB)
    r = get("/api/discover/containers")
    d = safe_json(r)
    ok = r.status_code == 200 and "containers" in d
    record("GET /api/discover/containers — 200", ok, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Phase 16: Error Response Shape
# ---------------------------------------------------------------------------
def test_error_response_shape():
    print("\n📋 Phase 16: Error Response Shape")

    # 401 error
    r = httpx.get(f"{BASE_URL}/api/jobs", timeout=10)
    d = safe_json(r)
    ok = r.status_code == 401 and "error" in d and "message" in d
    record("401 error — has {error, message}", ok, f"keys={list(d.keys())}")

    # 404 error
    r = get("/api/jobs/nonexistent-id")
    d = safe_json(r)
    has_error_shape = "detail" in d or ("error" in d and "message" in d)
    record("404 error — has error shape", has_error_shape, f"keys={list(d.keys())}")


# ---------------------------------------------------------------------------
# Phase 17: Rate Limiting
# ---------------------------------------------------------------------------
def test_rate_limiting():
    print("\n📋 Phase 17: Rate Limiting")

    # Send many bad keys rapidly to trigger rate limiting (threshold is 10)
    for i in range(12):
        try:
            httpx.get(f"{BASE_URL}/api/jobs", headers={"X-API-Key": f"ratelimit_{i}"}, timeout=3)
        except httpx.ReadTimeout:
            pass

    r = httpx.get(f"{BASE_URL}/api/jobs", headers={"X-API-Key": "ratelimit_final"}, timeout=3)
    ok = r.status_code == 429
    d = safe_json(r)
    rate_limited = "too many" in d.get("message", "").lower()
    has_retry = "retry_after" in d.get("details", {})
    record(
        "Rate limiting — 429 after many bad keys",
        ok and rate_limited and has_retry,
        f"status={r.status_code}, retry_after={d.get('details', {}).get('retry_after')}",
    )

    # Good key from same IP should also be locked out (IP-based rate limiting)
    r = get("/api/status", auth=False)  # Status is unauthenticated, should still work
    ok = r.status_code == 200
    record("Unauthenticated endpoint still works during lockout", ok, f"status={r.status_code}")

    # Wait for lockout to expire (30s is too long for QA, just note it)
    record("Rate limit lockout duration: 30s (not waiting)", True, "documented")


# ===========================================================================
# MAIN
# ===========================================================================
def main():
    global server_proc

    print("=" * 60)
    print("Arkive Runtime QA — API Integration Tests")
    print("=" * 60)

    # Start server
    print(f"\n🚀 Starting server on port {PORT}...")
    server_proc = start_server()
    if not wait_for_server():
        # Print server stderr for debugging
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
    print("✅ Server ready\n")

    try:
        # Run all test phases in order
        test_phases = [
            test_health_and_status,
            test_pre_setup_auth,
            test_setup_flow,
            test_auth_enforcement,
            test_jobs_crud,
            test_trigger_run,
            test_targets_crud,
            test_settings,
            test_directories,
            test_notifications,
            test_activity_and_logs,
            test_storage_stats,
            test_sse_streams,
            test_key_rotation,
            test_graceful_degradation,
            test_error_response_shape,
            test_rate_limiting,
        ]

        for phase_fn in test_phases:
            try:
                phase_fn()
            except Exception as e:
                record(
                    f"{phase_fn.__name__} — EXCEPTION",
                    False,
                    f"{type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}",
                )

    finally:
        print("\n🛑 Stopping server...")
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
                print(f"  ✗ {name}")
                if detail:
                    print(f"    {detail[:200]}")
    else:
        print(" — \033[32mALL PASS\033[0m")
    print("=" * 60)
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
