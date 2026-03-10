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

Branch:
- `main`

Recent important commits:
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

### 10. Prior deep QA and hardening already completed before the latest public cleanup

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

## Repo Hygiene Rules

This is a public repository. Keep it clean.

Do not commit:
- tokens
- passwords
- host access details
- internal-only troubleshooting notes
- private infrastructure identifiers unless they are already generic test fixtures

If you add handoff docs:
- keep them product- and code-focused
- avoid operational secrets

## Known Watch Areas

Not current blockers, but worth monitoring:
- transient database warnings in logs that still end in successful final dumps
- user confusion when target config points to a bucket/path different from what they expect
- CI duration if more integration-heavy coverage gets added
- future regressions where backup-time discovery and status aggregation drift apart again

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
- the recent CI failure had been fixed by `4f5eb86`
- all open PRs had been closed
- the app had live-validated fixes for:
  - hostname reporting
  - stale degraded dashboard state
  - scheduled backup collision semantics
  - Postgres companion discovery
  - activity feed badge rendering
  - first-boot bootstrap stability

If something in one of those areas looks broken again, start by checking for a regression against those fixes rather than assuming a new unrelated problem.
