# Arkive Agent Context

This file is a practical handoff for future coding agents working in this repository.

It is intentionally public-safe:
- no credentials
- no private infrastructure access details
- no internal-only runbooks

## Product Summary

Arkive is an open-source backup and disaster recovery system for Unraid Docker workloads.

Primary product behavior:
- discover Docker containers and their attached databases
- create application-consistent database dumps where needed
- back up dumps plus selected config/app paths with restic
- store snapshots on local or remote targets through provider integrations
- support restore planning and restore execution
- expose the platform through:
  - FastAPI backend
  - Svelte frontend
  - Python CLI

This repo is now the clean public repository:
- `https://github.com/kemetlabs/Arkive`

## Current Repo State

Branch:
- `main`

Recent important commits:
- `554ed4c` `fix: return total_snapshots from status endpoint`
- `5df66a6` `fix: replace personal hostname in test fixtures`
- `344684f` `feat: smart directory discovery with one-click add`
- `4f5eb86` `fix: avoid bandit SQL warning in conflict status update`
- `e925cd3` `fix: skip scheduled backup collisions`
- `94ffc86` `fix: persist discovery refresh and normalize target config`
- `07b120c` `fix: render activity type badges without fallback icons`
- `a631d10` `fix: resolve postgres companion containers in discovery`
- `ecc28d5` `fix: harden first-boot bootstrap and improve schedule UX`

Public-repo cleanup already happened:
- hosted/paid product surface was removed
- internal planning docs were removed
- screenshot/gallery docs were removed
- website subtree was removed
- stale public links, badges, and template metadata were corrected

## Architecture Map

Backend entrypoints and high-signal files:
- `backend/app/main.py`
- `backend/app/api/status.py`
- `backend/app/api/auth.py`
- `backend/app/api/discover.py`
- `backend/app/api/targets.py`
- `backend/app/api/jobs.py`
- `backend/app/api/restore.py`
- `backend/app/services/orchestrator.py`
- `backend/app/services/discovery.py`
- `backend/app/services/backup_engine.py`
- `backend/app/services/restore_plan.py`

Shared helpers added during this QA cycle:
- `backend/app/services/host_identity.py`
- `backend/app/services/discovery_persistence.py`

Frontend files most recently relevant:
- `frontend/src/routes/+page.svelte`
- `frontend/src/routes/setup/+page.svelte`
- `frontend/src/routes/activity/+page.svelte`
- `frontend/src/routes/settings/directories/+page.svelte`
- `frontend/src/lib/components/shared/StatusBadge.svelte`
- `frontend/src/lib/utils/schedule.ts`

CI/build/deploy files worth checking early:
- `.github/workflows/build.yml`
- `Dockerfile`
- `backend/pyproject.toml`
- `backend/tests/conftest.py`

## Major Work Completed

### 1. Public repo cleanup and product-surface cleanup

Completed:
- removed internal-only docs and planning artifacts from the public repo
- removed hosted/paid-only surface from product code and UI
- removed the website subtree
- corrected README, contribution docs, template metadata, and OCI metadata
- cleaned stale repo references and bad public links

Important implication:
- keep future commits public-safe
- do not reintroduce internal hostnames, tokens, or private runbooks

### 2. First-boot bootstrap hardening

Problem:
- empty or not-yet-ready DB state could break first boot
- `/api/status` and `/api/auth/session` could 500 instead of guiding setup

Fix:
- tolerate missing settings/bootstrap state
- return setup mode correctly instead of erroring

Relevant files:
- `backend/app/api/status.py`
- `backend/app/api/auth.py`
- `backend/app/core/dependencies.py`

Why it matters:
- first boot is part of the real product path, not a dev edge case

### 3. Activity feed badge bug

Problem:
- activity rows showed `?` icons for valid event types like `system`, `backup`, and `target`

Cause:
- the UI passed activity categories into a badge component that only understood run-state labels like `success`, `failed`, and `running`

Fix:
- explicit activity type mappings were added in `StatusBadge`

Relevant files:
- `frontend/src/lib/components/shared/StatusBadge.svelte`
- `frontend/src/lib/components/shared/StatusBadge.test.ts`

### 4. Schedule readability in setup and dashboard

Problem:
- setup and dashboard surfaced cron strings in a raw, low-signal way

Fix:
- schedule formatting was made more human-readable
- setup now reuses a better cron editor path
- dashboard job cards show readable schedule text with raw cron as secondary detail

Relevant files:
- `frontend/src/routes/+page.svelte`
- `frontend/src/routes/setup/+page.svelte`
- `frontend/src/routes/settings/jobs/+page.svelte`
- `frontend/src/lib/utils/schedule.ts`

### 5. False Postgres discoveries on app containers

Problem:
- app containers such as Immich and Paperless were sometimes treated as dump-capable Postgres containers
- this caused false partial backups because Arkive attempted `pg_dump` in app containers instead of the real DB containers

Fix:
- discovery now resolves companion Postgres containers using:
  - compose project/service metadata
  - DB host env hints
  - `depends_on` hints
- if no real companion can be found, Arkive skips the fake app-container Postgres discovery rather than generating a false failure

Relevant files:
- `backend/app/services/discovery.py`
- `backend/tests/unit/test_discovery.py`

Observed product effect:
- a previous live run was `partial` because of false Postgres entries
- after this fix, the next live run completed with all discovered DBs dumped successfully

### 6. Hostname showing Docker container ID

Problem:
- status showed values like a container ID instead of the actual host name

Cause:
- `socket.gethostname()` inside Docker resolves to the container hostname

Fix:
- hostname resolution now prefers:
  1. configured `server_name`
  2. Docker daemon host name from the mounted Docker socket
  3. container hostname fallback

Relevant files:
- `backend/app/services/host_identity.py`
- `backend/app/api/status.py`

Important behavior:
- this is generic, not hardcoded to one server

### 7. Healthy backup but degraded dashboard

Problem:
- dashboard DB health could stay degraded even after a successful backup

Cause:
- backup-time discovery refreshed in memory but did not persist refreshed results to `discovered_containers`
- status then read stale cached discovery data

Fix:
- added a shared persistence helper for discovery results
- backup orchestrator now persists refreshed discovery before dumps
- API discovery and scheduler discovery use the same persistence logic

Relevant files:
- `backend/app/services/discovery_persistence.py`
- `backend/app/services/orchestrator.py`
- `backend/app/api/discover.py`
- `backend/app/services/scheduler.py`
- `backend/tests/integration/test_status_consistency.py`

Why it matters:
- status summaries must reflect the same authoritative discovery data used during backup

### 8. Scheduled backup collision semantics

Problem:
- two scheduled jobs on the same minute could collide on the single global backup lock
- one would run, the other would be recorded as a failure

Fix:
- scheduled lock conflicts now persist as `skipped`
- manual/API trigger conflicts still remain `failed`

Relevant files:
- `backend/app/services/orchestrator.py`
- `backend/tests/unit/test_orchestrator_conflicts.py`

Why it matters:
- scheduled contention should not look like a real backup failure

### 9. Target config normalization

Problem:
- provider config values such as bucket names could be stored with leading or trailing whitespace

Fix:
- target config strings are normalized before validation and persistence

Relevant files:
- `backend/app/api/targets.py`
- `backend/tests/integration/test_api_targets.py`

Why it matters:
- provider misconfig is hard enough already; silent whitespace drift makes it worse

### 10. Smart directory discovery with one-click add

Problem:
- users had to manually find and type directory paths to add them to watched directories
- no guidance on which directories are worth backing up vs. massive re-downloadable media stores

Fix:
- `POST /directories/scan` now dynamically discovers all `/mnt/user/` subdirectories
- classifies by name (`SKIP_NAMES`), size (`SMALL_DIR_THRESHOLD`), and content (`_is_media_dominated`)
- returns prioritized suggestions with size estimates, file counts, and recommended exclude patterns
- frontend shows one-click "Add" buttons with priority badges (critical/recommended/optional)

Key design decisions:
- blocking filesystem I/O runs in `asyncio.to_thread()` to avoid blocking the event loop
- classification helpers are module-level functions for testability
- "photos" is NOT in `SKIP_NAMES` — personal photos are irreplaceable
- well-known paths (`known_paths`) only include genuinely backup-worthy dirs (appdata, docker, domains) — NOT media/isos, which must go through the normal skip-name/media-dominated filtering
- frontend updates local state after add/remove instead of re-scanning the filesystem

Relevant files:
- `backend/app/api/directories.py` (scan endpoint + classification helpers)
- `frontend/src/routes/settings/directories/+page.svelte`
- `frontend/src/lib/api/mock.ts` (mock must return `suggestions` key)
- `backend/tests/unit/test_directory_classification.py`
- `backend/tests/integration/test_api_directory_suggestions.py`

Lessons learned during review:
- do NOT put media/isos directories in `known_paths` — they bypass filtering and create bad suggestions
- unit tests must import from production code, not duplicate constants
- frontend must save path variables before clearing form fields if the path is needed later

### 11. Prior deep QA and hardening already completed before the latest public cleanup

Earlier work in this repo lifecycle already fixed and validated other important issues, including:
- MinIO/S3 repo-path correctness
- restore error persistence
- cancellation behavior for in-flight backup subprocesses
- stale backup lock cleanup on restart
- restore/backup conflict behavior
- directory path validation
- readonly-target validation edge cases
- broader backend suite reliability and speed improvements

Those fixes are in git history even if they are not all repeated in the short recent-commit list above.

### 12. Production hardening pass (v0.3.0)

Found by auditing a long-running production install. Before this pass, Arkive
reported `success` for weeks while backing up **no databases at all**.

Root causes and fixes:
- **Docker client never recovered from a boot-time timeout.** `docker.from_env()`
  handshakes at construction; one timeout at startup left `discovery`/`db_dumper`
  as `None` until the next container restart, and the orchestrator silently
  skipped dumps. Fix: `services/docker_client.py` (`ResilientDockerClient`, a lazy
  self-reconnecting proxy). Discovery retries (`discovery_attempts`) and a failure
  now makes the run `partial` with the reason in `job_runs.error_message`.
- **Silent-success paths removed.** A run is `partial` when discovery fails, when
  the DB count drops to 0 from a previous non-zero run, when a backup path is
  missing (restic silently drops missing paths), when restic exits 3 (snapshot
  written, some files unreadable), or when there are no enabled targets.
  `error_message` is populated for partial runs.
- **Stream exit codes.** docker-py `exec_run(stream=True)` always returns
  `exit_code=None`. Dumps now use the low-level exec API (`exec_create` /
  `exec_start` / `exec_inspect`) and require the pg_dump/mysqldump completion
  trailer; truncated dumps fail and their files are deleted.
- **Job types are honored.** `db_dump` refreshes DB dumps, `flash` refreshes the
  flash archive, `full` does both. All types upload the same path set (dump dir +
  watched dirs) so every snapshot is a complete restore point and restic retention
  groups them together.
- **Scheduled collisions queue instead of skipping** (`scheduled_lock_wait_seconds`).
  The default setup schedules made the flash job skip every day and the weekly
  verification skip every week. Weekly prune holds the backup lock; verify waits.
- **Dump directory hygiene.** `cleanup_dump_dir` keeps the newest dump per DB,
  removes dumps older than 7 days (vanished containers) and orphaned SQLite
  sidecars, and runs even when Docker is down. Duplicate companion DBs are
  deduped; same-basename SQLite files get distinct dump names.
- **Stable restic host name.** Resolution order: `server_name` setting → Docker
  daemon name (cached) → last daemon name → Unraid `ident.cfg` → container
  hostname. Container IDs leaking in split restic retention groups.
- **Noise.** The 5-minute target health check only logs status changes (migration
  4 purges old entries). Logs are valid JSON with true UTC timestamps.
- **Restic coordination.** `--retry-lock` on backup/forget/check; post-backup
  forget no longer prunes (weekly job prunes); `--exclude-if-present .arkive-exclude`
  plus `--exclude-caches` keep Arkive's own repo/restore dirs out of watched parents.
- **Local targets** on the container's ephemeral filesystem are rejected
  (`utils/mounts.py`).

Tests: `tests/unit/test_backup_pipeline_hardening.py`,
`tests/unit/test_dump_hardening.py`, `tests/unit/test_resilience_helpers.py`.

Rules that follow from this:
- never add a code path where a backup phase is skipped without either a
  warning that makes the run `partial` or an explicit, documented reason
- test doubles for dumps must emit realistic output (including trailers)
- tests that exercise scheduled conflicts must pass `lock_wait_seconds=0`

### 13. Knowing the backups work (v0.4.0)

The v0.3.0 failure went unnoticed for months because nothing outside a run
looked at whether backups were still happening. This pass adds checks that
don't depend on a run reporting its own failure.

- **Freshness watchdog** (`services/freshness.py`, scheduler job
  `system_backup_freshness`, every 15 min). A job is stale when its last *good*
  run is older than its longest cron gap plus 25% (at least 2 h), or the
  `stale_backup_hours` override. Good means `success`, or `partial` with a
  snapshot written and, for DB jobs on a system that has had databases, at
  least one dump. Stale jobs make `/api/status` `status: "error"`
  (`backup_freshness` field) and notify `backup.stale` (repeated daily).
- **Heartbeat** (`heartbeat_url` setting). The freshness job GETs it only while
  every job is healthy, so an external monitor (Uptime Kuma push,
  healthchecks.io) alerts when Arkive is down *or* backups are bad.
- **Restore drills** (`services/restore_drill.py`, `api/drills.py`, migration 5
  tables `restore_drills` / `restore_drill_results`, setting-driven weekly job).
  Restores recent dumps from the latest snapshot and loads them into a
  throwaway, network-less container of the source image.
- **Recovery kit** (`services/recovery_kit.py`, `POST /api/settings/recovery-kit`).
  Password + repository locations + restore steps; `/api/status`
  `recovery_kit_confirmed` drives a dashboard nag until confirmed (reset when the
  encryption password changes).
- **Keyfile guard** (`core/security.verify_keyfile`, called at startup). Refuses
  to start when `/config/.keyfile` is missing, corrupt or can't decrypt the
  stored secrets, instead of silently generating a new key.
  `ARKIVE_ALLOW_NEW_KEYFILE=1` overrides.
- **Dumps deduplicate.** `dump_compression` defaults to `none` (plain `.sql`;
  restic compresses). Every database in a Postgres cluster is dumped, plus
  roles via `pg_dumpall --globals-only`. `arkive-dumps-manifest.json` in the
  dump dir maps files to containers/databases (drills read it from snapshots).
- **Notification events** have one catalog (`services/notification_events.py`,
  `GET /api/notifications/events`); channel create/update rejects unknown events.
  The UI previously offered events the backend never sent, and the backend sent
  events no UI subscription could match.
- **Legacy host names.** Weekly retention re-labels Arkive snapshots recorded
  under a container ID (`restic rewrite --new-host --forget`) so old retention
  groups merge and get thinned.
- **Read-only data mounts.** Restores only go to `/config/restores` (model
  validation) and are refused with a clear 400 when the destination is on a
  read-only mount, so `/mnt/user` and the flash config can be mounted `:ro`.
- uvicorn's own log lines are JSON too (`backend/logging.json`, `--log-config`).

Tests: `tests/unit/test_watchdog_recovery.py`, `tests/unit/test_restore_drill.py`,
`tests/integration/test_api_drills.py`, `tests/unit/test_dump_formats.py`.

Rules that follow from this:
- every event the backend sends must be in `NOTIFICATION_EVENTS`, and the UI
  must take its list from the API
- new status signals that mean "backups are not happening" belong in
  `evaluate_freshness`, so the dashboard, notifications and heartbeat agree

Adversarial review of v0.4.0 (two independent reviewers) found data-loss edge
cases; the fixes are now invariants:
- **Destructive repository operations need proof of ownership.** Snapshot
  re-labelling only touches snapshot IDs this instance recorded in
  `job_run_targets`; a host-name pattern or a shared path is not proof.
- **Never write marker files into user-configured directories.** Arkive's own
  storage (restore dir, each `arkive-repo`) is excluded by exact `--exclude`
  paths, their Docker host-path aliases (the same folder is often visible as
  `/config/...` and `/mnt/user/appdata/arkive/...`), and a marker inside those
  Arkive-owned directories only (`_internal_excludes`).
- **Startup sweeps delete only what carries Arkive's ownership marker**
  (drill dirs: `.arkive-drill` with the drill id), never by name prefix.
- **Dump files:** names carry an identity hash (no two databases share a
  cleanup group); writers publish via `.partial` + atomic rename; cleanup never
  ages out the newest dump of a still-discovered database and skips age
  deletion entirely when discovery was degraded; manual dumps take the backup
  lock.
- **A database that stops being backed up is a warning until it returns** (or
  `database_loss_window_days` pass), and Postgres enumeration failures surface
  as run warnings rather than silently shrinking coverage.
- **Errors are not empty results.** Use `BackupEngine.list_snapshots()` (raises
  `ResticError`) wherever "no snapshots" would lead to a skip or a deletion.

## Validation Summary

This codebase was exercised well beyond unit-only confidence.

Validated areas during the recent QA cycle included:
- first-boot setup flow
- discovery
- manual backups
- scheduled backups
- remote target validation
- restore behavior
- status/health summaries
- activity feed rendering
- CLI behavior
- frontend E2E paths

Target/provider coverage exercised across the broader QA effort included:
- local
- SFTP
- B2
- MinIO / S3-compatible

Important product observations that remain worth remembering:
- database-aware dumps are required for reliable disaster recovery
- raw appdata copy alone is not a safe replacement for live database dumps
- discovery, backup orchestration, and status summarization must stay aligned
- scheduler behavior should preserve user trust by distinguishing skip vs fail correctly

## CI and Test Notes

Backend CI is intentionally split:
- core lane: `pytest -m "not slow and not live"`
- heavy lane: `pytest -m "slow and not live"`

Marker logic and supporting config live in:
- `backend/tests/conftest.py`
- `backend/pyproject.toml`

Recent CI issue fixed:
- Bandit flagged a string-built SQL update in `orchestrator.py`
- fixed in `4f5eb86` by parameterizing the query

Relevant recurring commands:
- backend core:
  - `cd backend && .venv/bin/python -m pytest -m "not slow and not live" -q`
- backend heavy:
  - `cd backend && .venv/bin/python -m pytest -m "slow and not live" -q`
- Bandit:
  - `cd backend && .venv/bin/bandit -r app -ll`
- frontend checks:
  - `cd frontend && npm run check`
  - `cd frontend && npm run build`

Performance note:
- a meaningful optimization pass was already done on the backend suite by reusing safe shared test harnesses in high-cost modules
- if runtime regresses again, benchmark the core lane first before changing fixture scope blindly

## Agent Guidance

If future work touches backup, discovery, status, or scheduling, read these first:
- `backend/app/services/discovery.py`
- `backend/app/services/discovery_persistence.py`
- `backend/app/services/orchestrator.py`
- `backend/app/api/status.py`
- `backend/app/api/targets.py`
- `backend/app/services/host_identity.py`

If future work touches first-boot or auth:
- `backend/app/api/auth.py`
- `backend/app/core/dependencies.py`
- `backend/app/api/status.py`

If future work touches schedule UI or dashboard summaries:
- `frontend/src/routes/+page.svelte`
- `frontend/src/routes/setup/+page.svelte`
- `frontend/src/lib/utils/schedule.ts`

If future work touches directory discovery or watched directories:
- `backend/app/api/directories.py`
- `frontend/src/routes/settings/directories/+page.svelte`
- do NOT add media/isos paths to `known_paths` — they must go through skip-name filtering
- ensure `_is_media_dominated` and `SKIP_NAMES` stay module-level for testability
- keep blocking I/O in `asyncio.to_thread()`

When changing discovery behavior:
- verify discovered results themselves
- verify backup-time discovery persistence
- verify status aggregation against the persisted discovery data

When changing scheduler behavior:
- verify scheduled and manual triggers separately
- preserve clear semantics for `skipped`, `failed`, and `cancelled`

When changing provider config handling:
- normalize before validation and persistence
- do not preserve accidental whitespace
- verify both target test behavior and real backup path construction

When changing UI status rendering:
- verify icon mapping and empty states
- verify the dashboard is not showing stale data after backend fixes
- **every field the frontend reads from `/api/status` must exist in the return dict** — if the frontend reads `data.status?.foo`, the backend must return `foo`. Check `frontend/src/routes/+page.svelte` and `frontend/src/lib/api/mock.ts` for expected fields.
- the mock in `mock.ts` must also include any new fields so demo mode works

## Repo Hygiene Rules

This is a public repository. Keep it clean.

Do not commit:
- tokens
- passwords
- host access details
- internal-only troubleshooting notes
- private infrastructure identifiers unless they are already generic test fixtures
- real server hostnames (e.g., "AdamTower" was found and replaced with "test-server" — do not reintroduce)
- real IP addresses of the developer's infrastructure
- seedbox credentials or private tracker details

Test fixtures must use generic values:
- hostnames: `test-server`, `tower`, `unraid-test`
- IPs: `192.168.1.100` (standard example only in placeholder text)
- users: `alice`, `bob`, `test-user`
- never use real personal identifiers even in test code

If you add handoff docs:
- keep them product- and code-focused
- avoid operational secrets

Before submitting PRs:
- grep the diff for personal hostnames, IPs, credentials
- security scanning should be a standard part of the PR process

## Known Watch Areas

- a run that is `partial` every time usually means configuration drift (a watched
  directory that no longer exists, a target that was disabled) — the message in
  `job_runs.error_message` says which

Not current blockers, but worth monitoring:
- transient database warnings in logs that still end in successful final dumps
- user confusion when target config points to a bucket/path different from what they expect
- CI duration if more integration-heavy coverage gets added
- future regressions where backup-time discovery and status aggregation drift apart again
- frontend/backend field mismatches: the "0 snapshots" bug was caused by the frontend reading `total_snapshots` from a status response that never included it — always cross-check both sides when adding dashboard data

## Fast Resume Checklist

If picking this repo up cold:
1. read this file
2. inspect the recent commits listed above
3. inspect `backend/app/services/orchestrator.py`
4. inspect `backend/app/services/discovery.py`
5. inspect `backend/app/api/status.py`
6. inspect `backend/app/api/targets.py`
7. run focused tests for the touched area before broadening out

## Last Known Intent

At the time this handoff was updated:
- public repo cleanup was complete
- smart directory discovery feature was merged (PR #26)
- personal hostname "AdamTower" was cleaned from all test fixtures
- security scan confirmed: no credentials, private keys, or personal data remain in the repo
- the app had live-validated fixes for:
  - hostname reporting
  - stale degraded dashboard state
  - scheduled backup collision semantics
  - Postgres companion discovery
  - activity feed badge rendering
  - first-boot bootstrap stability
  - smart directory discovery and one-click add
  - snapshot count display on dashboard (was returning 0 due to missing field in status API)

If something in one of those areas looks broken again, start by checking for a regression against those fixes rather than assuming a new unrelated problem.
