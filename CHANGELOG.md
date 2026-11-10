# Changelog

All notable changes to Arkive are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Arkive uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
When a version is tagged, its section here becomes the GitHub release notes
(see [RELEASING.md](RELEASING.md)).

## [Unreleased]

### Changed
- The `islamdiaa/arkive:latest` image now moves only when a new version is released. Until now every push to `main` republished `latest`, so the Unraid template and the example Compose file could pull unreleased code. Builds of `main` are still published as `main` and as the commit SHA.
- GitHub releases are created from this changelog when a version is tagged.
- Dependency updates: click 8.5.0, and the Inter and JetBrains Mono fonts.

## [0.4.0] - 2026-10-30

Arkive 0.4.0 is about one question: do my backups actually work? It follows a production audit that found 0.2.4 reporting `success` for months while backing up no databases at all, and it adds checks that don't rely on a backup run reporting its own failure. Version 0.3.0, the production hardening from that audit, was never tagged on its own; it ships here.

Upgrade by recreating the container with the same `/config` volume; database migrations run on start.
- **Keep `/config/.keyfile`.** 0.4.0 refuses to start without the keyfile that encrypted your stored secrets. Set `ARKIVE_ALLOW_NEW_KEYFILE=1` only if you intend to start over and re-enter them.
- Data can now be mounted read-only (`/mnt/user/appdata:/mnt/user/appdata:ro`, `/boot/config:/boot-config:ro`); Arkive no longer writes there.
- After upgrading, save the recovery kit (Settings → Recovery kit) and add a notification channel.

### Added
- **Overdue-backup watchdog.** A job whose last good backup is older than its schedule window turns the dashboard red and sends `backup.stale` alerts.
- **Heartbeat.** Point Arkive at an Uptime Kuma push monitor or a healthchecks.io check. It is pinged only while every job is healthy, so you also hear about it when Arkive itself is down.
- **Weekly restore drills.** Recent database dumps are restored from the latest snapshot and loaded into a throwaway, network-isolated container of the same database image.
- **Recovery kit.** A one-time export of the encryption password, every repository location and the restore steps. The dashboard reminds you until you confirm you've saved it.
- **Keyfile guard.** Arkive refuses to start if `/config/.keyfile` is missing or can't decrypt the stored secrets, instead of silently creating a new key that would strand every stored secret.
- Every database in a PostgreSQL cluster is dumped, plus roles (`pg_dumpall --globals-only`). A manifest in the dump directory maps each dump file to its container and database.
- **Exclude a database** from backups on the Databases page (for example an app cache that holds an exclusive lock).
- Redis dumps support `AUTH`.
- One catalog of notification events (`GET /api/notifications/events`). The UI no longer offers events the backend never sends.
- A [security policy](SECURITY.md) with private vulnerability reporting.

### Changed
- **No silent successes.** A run that misses anything (a database, a watched path, a target, unreadable files, failed discovery) is `partial`, with the reason.
- Dumps are stored uncompressed by default, so restic deduplicates them: a large database adds only its changes to each snapshot. `gzip` mode uses `--rsyncable`.
- Every job type uploads the dumps and the watched directories, so any one snapshot is a complete restore point.
- Scheduled jobs that collide wait for each other instead of being skipped.
- The dump directory keeps the newest dump of each database; dumps of removed containers age out after 7 days.
- Restores always land in `/config/restores`, so the data you protect can be mounted read-only. A restore to a read-only destination gets a clear error.
- Local storage targets on the container's own, non-persistent filesystem are rejected.
- Logs, including uvicorn's, are JSON with UTC timestamps. The 5-minute target health check logs only status changes.
- restic 0.19.1 and rclone 1.75.2, built from source with a current Go toolchain.

### Fixed
- The Docker client reconnects after a startup timeout. In 0.2.4, one timeout disabled database dumps until the container was restarted.
- Dump exit codes are checked and truncated dumps are detected. A failed dump never replaces the last good one, and dump files are published atomically.
- SQLite copies from read-only mounts no longer drop data still in the write-ahead log.
- Weekday schedules (`* * 1-5`, `* * 0`) ran a day late.
- `db_dump`, `flash` and `full` jobs now each refresh what their type says.
- The restic host name is stable, so container IDs no longer split retention groups. Old snapshots are re-labelled during weekly retention.
- Arkive's own repository and restore folders are no longer backed up when they sit inside a watched directory.
- The light theme works across the whole UI.
- The sidebar shows the running version instead of `v0.1.0`.
- The restore-plan PDF referred to a container image that doesn't exist; it now uses Docker Hub.

### Security
- **Command injection in the snapshot browser.** An authenticated API caller could pass a path that restic read as an option (`--password-command`). Because Arkive holds the Docker socket, this amounted to control of the host. User-supplied values are now passed after `--` and validated.
- **API open before setup.** Until the setup wizard ran, every API route was reachable without credentials. Now only the scans the wizard needs are open, and setup can be completed only once, even under concurrent requests.
- Dependency updates for published advisories: cryptography 50.0.2, weasyprint 70.0, jinja2 3.1.6, python-multipart 0.0.32, apprise 1.13.1, pydantic-settings 2.16.2, zod 4.6.7. restic and rclone are rebuilt with a current Go toolchain, and the image picks up Debian security updates.

If you run 0.2.x on a network others can reach, upgrade.

## [0.2.4] - 2026-03-19

### Fixed
- The verification test restore asked restic for a file by name only, without its directory, so nothing was restored. It now uses the full path.

## [0.2.3] - 2026-03-19

### Fixed
- The verification test restore recognises restic's `directory` entries and looks up to three levels deep for a file to restore.

## [0.2.2] - 2026-03-19

### Fixed
- The verification test restore looked only at the snapshot root, which holds only directories, so the file check was always skipped and the trust score stayed around 80. It now looks in subdirectories.

## [0.2.1] - 2026-03-18

### Fixed
- **Verify Now** recorded a verification run but never started it, so the run stayed `running` and the trust score showed 0.

## [0.2.0] - 2026-03-18

### Added
- **Backup verification with a trust score** (0–100) on the dashboard. Every target is verified weekly: `restic check`, a test restore of a random file with a SHA-256 comparison, and validation of database dumps. A **Verify Now** button runs it on demand, and a failed verification sends a notification.
- **Directory discovery.** A scan of `/mnt/user` suggests shares worth backing up, with sizes and priorities, and adds one with a click. Media libraries and other re-downloadable data are left out.
- Warnings on the dashboard when appdata, the flash configuration or the watched directories aren't covered by a successful backup.
- `ARKIVE_CORS_ORIGINS` to set the allowed CORS origins.

### Changed
- Images are published to Docker Hub as `islamdiaa/arkive`.
- Schedules read as plain language in setup and on the dashboard, with the cron expression alongside.
- Cron expressions are validated in setup and when a job is created or changed.
- A scheduled backup that collides with a running one is recorded as `skipped` instead of `failed`.

### Fixed
- On first boot, `/api/status` and `/api/auth/session` returned errors instead of starting setup.
- App containers such as Immich and Paperless were treated as PostgreSQL servers, so Arkive ran `pg_dump` in the app container and the run ended `partial`. Discovery now finds the actual database container.
- Database health on the dashboard stayed degraded after a successful backup.
- Status showed the container ID instead of the server's host name.
- Storage target settings kept leading and trailing spaces (for example in bucket names).
- The activity feed showed `?` icons for system, backup and target events.
- The dashboard showed 0 snapshots.
- Inside the container, Unraid was reported as a generic Linux host. It is now recognised from the mounted flash configuration.
- On mobile, the sidebar covers the page with a backdrop and closes when you navigate.

### Security
- Credentials are redacted from database-dump and subprocess error logs.

## [0.1.0] - 2026-03-10

### Added
- First public release: discovery of Docker containers and their databases with app profiles; dumps of PostgreSQL, MariaDB/MySQL, MongoDB, SQLite and Redis; encrypted restic snapshots of the dumps, watched directories and Unraid flash configuration on local disk, Backblaze B2, S3-compatible, Wasabi, SFTP, Google Drive and Dropbox targets; cron schedules and retention; notifications; a snapshot browser, granular restore and a restore-plan PDF; a setup wizard, dashboard, REST API and CLI.

[Unreleased]: https://github.com/islamdiaa/Arkive/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/islamdiaa/Arkive/compare/v0.2.4...v0.4.0
[0.2.4]: https://github.com/islamdiaa/Arkive/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/islamdiaa/Arkive/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/islamdiaa/Arkive/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/islamdiaa/Arkive/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/islamdiaa/Arkive/compare/a4c1be7cd3f44b64dca5a30e8dd50256ccfecdf9...v0.2.0
[0.1.0]: https://github.com/islamdiaa/Arkive/tree/a4c1be7cd3f44b64dca5a30e8dd50256ccfecdf9
