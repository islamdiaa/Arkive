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
- `https://github.com/islamdiaa/Arkive`

## Current Repo State

Branch: `main` (v0.5.0). The v0.3.0 hardening and v0.4.0 "knowing the backups
work" releases landed together in PR #52; sections 12 and 13 below describe
them, and section 17 describes v0.5.0. Use `git log` for the commit history
and `CHANGELOG.md` for what each release changed for users.

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

### 14. Release hardening (v0.4.0 release)

Before the first public v0.4.0 release, a dependency/image scan and an
adversarial security review found:
- **restic option injection.** The snapshot browser passed a user-supplied path
  straight to `restic ls`; `--password-command=...` runs a command, and Arkive
  holds the Docker socket. User-supplied values now go after `--` or as
  `--flag=value`, and `_validate_path` / snapshot-ID validation reject a leading
  `-` and control characters.
- **Open API before setup.** `require_auth` used to let every request through
  until an API key existed. Now only the setup wizard's scans
  (`_SETUP_ROUTES` in `core/dependencies.py`) are open; everything else, SSE
  included, returns 401. Setup claims `api_key_hash` with a plain `INSERT`, so
  concurrent setup requests can't both win.
- **Vulnerable dependencies** (cryptography, weasyprint, jinja2,
  python-multipart, apprise, pydantic-settings, zod) and old restic/rclone
  binaries built with a vulnerable Go toolchain. The release workflow's Trivy
  step fails on fixable HIGH/CRITICAL findings, so scan the image locally
  before tagging.
- **Light theme.** `.light` in `frontend/src/app.css` only overrode the legacy
  tokens, so most panels stayed dark under dark text. Every token in `:root`
  needs a light value.

Rules that follow from this:
- never put a user-supplied value in a restic/rclone argv position where it can
  be parsed as an option
- a new route the setup wizard needs before setup goes in `_SETUP_ROUTES`;
  don't reopen the bootstrap bypass
- run `pip-audit`, `npm audit` and Trivy on the image before a release tag
- the full release checklist is `RELEASING.md`. A `vX.Y.Z` tag publishes the
  image and creates the GitHub release from that version's `CHANGELOG.md`
  section (`scripts/release.py`); Docker `latest` moves only on the newest
  stable tag, never on a push to `main`. CI runs the workflow from the tagged
  commit, so a maintenance branch cut from v0.4.0 or earlier must take this
  tooling first (RELEASING.md, "Fixes for an older release line"). User-visible
  changes get a line under `## [Unreleased]` in `CHANGELOG.md`

### 15. Restore path review with real engines

`scripts/restore-roundtrip/restore_roundtrip.py` runs dump -> backup ->
restore -> load (following the generated restore plan) and a drill against
Postgres 16/17, MariaDB 11.4, MySQL 8.4 and WAL-mode SQLite (and, since
the guided-restore work, Redis 6.2/7.4/8.0 with and without the append-only
file) inside a throwaway Docker-in-Docker daemon that publishes no host port
(see its README). Its first run found:
- pg_dump ran with `--no-owner --no-privileges`: a database owned by an app
  role came back owned by the restoring superuser, and the app got
  "permission denied" on its own tables. Dumps now keep owners and GRANTs;
  roles come from the globals dump, restored first (the drill loads it too,
  minus its own bootstrap superuser's CREATE/ALTER ROLE lines).
- mysqldump without `--routines --events` silently dropped procedures,
  functions and events; MySQL 5.7's default utf8 turned emoji into `?`.
- Only `MYSQL_DATABASE` was dumped from a MariaDB/MySQL server; every
  application database is now listed (like Postgres since v0.4.0).
- MariaDB/MySQL users and grants were not dumped at all (the app's user was
  missing after a restore onto a new server). Now each server gets an
  accounts dump (`services/mysql_accounts.py`, kind `accounts`, db_name
  `accounts`): `SHOW CREATE USER` + `SHOW GRANTS` per account (MySQL 8.0.17+
  with `print_identified_with_as_hex`; binary hashes are otherwise unsafe),
  written as `CREATE USER IF NOT EXISTS` + `ALTER USER` + grants + default
  roles. root, `mysql.*`/`mariadb.sys`, the image's `healthcheck` and
  anonymous accounts are left out (the new container's root password comes
  from its environment). Restored after the server's databases (table grants
  need the tables); drills skip it; it needs root credentials and is
  `skipped` with that reason otherwise.
- Drill false failures: a `CREATE TABLE` inside a function/procedure body was
  counted as a table, and a `mysql` system-schema dump (no `MYSQL_DATABASE`)
  was counted outside its own schema.
- Restore plan commands: `psql -U postgres` (fails when POSTGRES_USER is
  another role), `mysql -u root` (no such binary in MariaDB 11, no password),
  `< glob` (only bash expands it), SQLite copied to a guessed path while the
  old `-wal` stayed (SQLite replays it over the restored file), restic without
  `-it` (cannot ask for the password).
- `redact_credentials` turned `immich-postgres/...` into `immich-p[REDACTED]`.

Rules that follow from this:
- a restore must bring back what the app needs to run, not only its rows:
  owners, grants, roles, routines, events, MariaDB/MySQL accounts
- a non-database dump (Postgres `globals`, MariaDB/MySQL `accounts`) is told
  apart from a database of the same name by its identity hash
  (`dump_prefix(..., kind=...)`, `server_dump_keys`), never by its name
- restore plan commands are executed by the round trip; keep them runnable
  in POSIX sh against the container's own environment (and never pass a bare
  `-p` to a MySQL client: with no TTY it reads the password from the piped dump)
- the drill must not be stricter than the restore it checks: the roles dump
  is loaded without ON_ERROR_STOP and judged by SQLSTATE afterwards, so roles
  the image's own init scripts created ("already exists", 42710) don't fail it
- database-name validation rejects argv hazards only (leading `-`, control
  characters); MySQL names may contain dots (`example.com`) and slashes
- the round-trip harness deletes only a `--workdir` carrying its
  `.arkive-restore-roundtrip` marker, and refuses a non-empty unmarked one

### 16. Tailwind CSS 4 (#53)

- No `tailwind.config.ts`: `@tailwindcss/vite` plus CSS config in
  `frontend/src/app.css`. `@theme inline reference` maps colour utilities to the
  runtime tokens in `:root`/`.light`, so `bg-danger/10` works on variables
  without wrappers; `src/app.css.test.ts` checks every token has a light value
  and every utility colour points at a defined token.
- `bg-primary` and `bg-danger` use the darker `--color-*-fill` tokens (white
  text needs 4.5:1 on them), through `--background-color-*` theme keys.
  Gradient stops read the same keys, so a gradient that wants the accent
  itself names the token: `from-(--color-primary)`, not `from-primary`.
- `space-x/y-*` now put the margin on the end of every child but the last, and
  a child's own margin wins. Children with their own margins, inline labels
  next to inputs, and inline links after blocks render differently from v3.
- `frontend/scripts/visual-diff.sh` screenshots every route in demo mode (both
  themes, desktop and phone, plus opened forms and dialogs) from two builds and
  diffs them. Run it for CSS or frontend dependency changes.

### 17. What is still missing (v0.5.0)

v0.4.0 checks that backups happen. v0.5.0 adds what a healthy-looking install
can still lack, and a way back for one database.

- **Offsite awareness** (`services/offsite.py`, `/api/status` `offsite`, event
  `backup.no_offsite`, migration 7). Warns when every target the enabled jobs
  upload to is on this server. It follows the jobs' target lists, not the
  target table. It is a warning only and must stay out of `status`, the
  freshness verdict and the heartbeat. Detection errs towards silence, and a
  failed lookup keeps the last verdict instead of resetting it.
- **Per-database health** (`services/database_health.py`, migration 8:
  `job_run_databases.db_key`, `dumped_at`, `shrink_flagged`). The dump-shrink
  check makes a run `partial`; a flagged dump never joins the size baseline,
  so the warning lasts until the size returns or the user accepts it on the
  Databases page. Redis is exempt.
- **Setup checklist** (`api/setup_checklist.py`). Every step is judged from
  evidence in the database, never from a flag the UI sets. Dismissal is stored
  server-side and left out of config export.
- **Guided database restore** (`services/guided_restore.py`, table
  `guided_restores`, route `/restore/database`). Holds the backup lock from
  start to finish, takes a safety dump first (undo loads it back), and streams
  the dump into the container's own client over `docker exec` stdin: never
  through a shell, never with user input in an option position. SQLite files
  are never written by Arkive; the operation ends `action_required` with the
  steps to swap the file in.
- **Guided restore for MongoDB and Redis** (after v0.5.0). MongoDB is a
  `load` type: the archive is checked end to end first (`services/dump_checks.py`:
  every collection's EOF marker and CRC-64; `mongorestore --dryRun` reads no
  documents), the databases being replaced are dropped with `mongosh` and
  `mongorestore --archive --drop` reads the archive from stdin. A server
  discovered as `admin` (no `MONGO_INITDB_DATABASE`; the dumper dumps the
  whole server) means every database except admin/config/local, so undo
  (safety `mongodump` of exactly that set) is exact. Passwords: mongosh and
  mongodump read them on stdin (`--file=/dev/stdin`, `--config=/dev/stdin`),
  mongorestore from a file in a `mktemp -d` folder in the container's own
  /tmp (never a mounted one), argv only as a stated fallback. The backup
  dumper does the same for mongodump when `mongodump --help` lists
  `--config` (Database Tools 100.3+: mongo:4.4 and later images). Redis is a
  `manual` type with a `check_container` step: it reads `dir`, `dbfilename`,
  `appendonly`, `appenddirname`, the version and the data volume, checks the
  RDB (header, CRC-64, format the running Redis reads) and returns steps that
  rebuild the append-only file from the RDB. Moving the AOF aside alone makes
  Redis start empty, and Redis rewrites the RDB on shutdown (swap only while
  stopped). `scripts/restore-roundtrip/guided_roundtrip.py` proves both on
  real engines (MongoDB 7.0/8.0, Redis 6.2/7.4/8.0).
- **Accessibility** (`frontend/tests/e2e/a11y.spec.ts`): axe runs on every
  route in both themes at desktop and phone widths.
- **Unraid Community Applications** (`unraid-template.xml`, `ca_profile.xml`,
  `docs/unraid-community-apps.md`): the template uses `islamdiaa/arkive:0`.

Rules that follow from this:
- a signal about where backups go or how the install is configured is a
  warning; only "backups are not happening" belongs in `evaluate_freshness`
- guided restore steps for a manual type are run as written by the guided
  round-trip harness; keep them POSIX sh and re-runnable (the safety copy is
  restored by running the same steps with its path)
- a new page goes into the axe route list and into `ROUTES` in
  `frontend/tests/visual/routes.spec.ts`; don't dim text with `opacity-*`
  (it fails contrast), use the secondary text colour
- the next schema migration is 9

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

CI (`.github/workflows/build.yml`) runs on every PR:
- `lint`: ruff check and ruff format --check (rules in `backend/pyproject.toml`)
- `test-backend-core`: `pytest -m "not slow and not live" -n auto`
- `test-backend-heavy`: `pytest -m "slow and not live" -n auto` (Docker fakes, providers)
- `test-critical`, `test-frontend` (svelte-check + build), `test-components` (Vitest)
- `frontend-e2e`: the Playwright suite via `frontend/scripts/e2e.sh`
- `sast`: bandit
- `build`: multi-arch image; pushed to Docker Hub only from `main` and tags

CI runners are not root and run pytest under xdist: tests must not depend on
the effective uid, on test order, or on files in the working directory (the
suite fails if a test creates files named after a mocked config path; rerun
with `ARKIVE_TRACE_MOCK_ARTIFACTS=1` and without `-n` to find it).

Marker logic and supporting config live in:
- `backend/tests/conftest.py`
- `backend/pyproject.toml`

Local commands (or the `make` targets: `test`, `test-slow`, `frontend-check`,
`test-e2e`, `lint`):
- backend core: `cd backend && python -m pytest -m "not slow and not live" -n auto`
- backend heavy: `cd backend && python -m pytest -m "slow and not live" -n auto`
- bandit: `cd backend && bandit -r app -ll`
- frontend: `cd frontend && npm run check && npx vitest run && npm run build`
- end-to-end: `frontend/scripts/e2e.sh --docker`
- visual diff of two frontend builds: `frontend/scripts/visual-diff.sh --docker <baseline-build-dir>`

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
- real server hostnames (one was once found in test fixtures and replaced with `test-server`; never name it, or any other real host, anywhere in the repo — including in this file)
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

At the time this handoff was updated (v0.5.0):
- v0.3.0/v0.4.0 were merged (PR #52) after two rounds of adversarial review;
  the invariants that came out of it are listed at the end of section 13
- the long-running production install that motivated section 12 runs v0.4.0
  and dumps all of its databases again
- section 14 fixes landed before the v0.4.0 tag (the first GitHub release)
- v0.5.0 (section 17) was built as separate PRs, each reviewed adversarially;
  MariaDB/MySQL users and grants are dumped (the accounts dump, see section
  15). Guided restore now covers MongoDB (in place) and Redis (prepared,
  swapped in by hand); MongoDB sharded clusters, secondaries and pre-6.0
  images (no mongosh) are refused. The restore plan's Redis steps use the
  same commands (`services/redis_swap.py`) and are run by
  `restore_roundtrip.py` on Redis 6.2/7.4/8.0 with and without the
  append-only file; backups give mongodump its password on stdin
  (`--config=/dev/stdin`, Database Tools 100.3+, argv only on the legacy
  4.2 tools)
- the frontend is on Tailwind CSS 4 (#53; see the Tailwind section above)
- the frontend is on SvelteKit 3: its config lives in `frontend/vite.config.js`
  (there is no svelte.config.js), `$lib` still works through the deprecated
  `alias` option (moving to `#lib` subpath imports is a follow-up), and `$:`
  components read the route from `$lib/stores/page` because `$app/stores` is
  gone and `$app/state` only notifies runes. The one npm advisory left is
  deepmerge (GHSA-gcqx-w48m-w2h4, via vite-plugin-svelte, build-time only): no
  patched release exists and its CVE was rejected upstream
- earlier live-validated fixes (hostname reporting, dashboard/discovery
  consistency, scheduled collisions, Postgres companion discovery, activity
  badges, first-boot stability, directory discovery, snapshot counts) still
  apply

If something in one of those areas looks broken again, start by checking for a regression against those fixes rather than assuming a new unrelated problem.
