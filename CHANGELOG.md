# Changelog

All notable changes to Arkive are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and Arkive uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
When a version is tagged, its section here becomes the GitHub release notes
(see [RELEASING.md](RELEASING.md)).

## [Unreleased]

### Added
- **Guided restore for MongoDB and Redis.** MongoDB is restored in place like PostgreSQL and MariaDB: Arkive checks every collection's checksum in the archive first, takes a safety dump, drops the database (or, for a server dumped whole, every database except `admin`, `config` and `local`) and loads the archive with `mongorestore`; Undo puts the safety dump back the same way. The MongoDB password never appears on a command line. Redis is prepared like SQLite: Arkive restores the `.rdb` file, checks its checksum and that the running Redis can read it, saves the current data and gives you the exact steps to swap it in, including a new append-only file when `appendonly` is on (moving the old one aside alone makes Redis start empty).

### Fixed
- The manual MongoDB and Redis restore steps in the restore guide: a `.archive.gz` dump is decompressed with `gunzip -c` (not `mongorestore --gzip`), and Redis with the append-only file needs a new one made from the dump.

## [0.5.0] - 2026-11-10

Arkive 0.5.0 answers the questions that come after "do my backups work?": is there a copy somewhere else, is each database healthy, what is still missing after setup, and how do I get one database back without doing it by hand. It is also the first release prepared for Unraid Community Applications.

Upgrade by recreating the container with the same `/config` volume; database migrations run on start.
- If every storage target your jobs upload to is on the server itself, the dashboard now says there is no offsite copy. It is a warning only: it does not change the system status or the heartbeat. Notification channels that already get backup failures also get the new "No offsite copy" event.
- A database whose dump suddenly shrinks to half its usual size or less now makes that run `partial` until the size comes back or you accept the new size on the Databases page.
- PostgreSQL dumps now carry owners and grants. Restore the roles dump (`globals`) before a database dump, as the restore plan describes.
- The Unraid template and the example Compose file follow release tags (`islamdiaa/arkive:0` and `latest`), no longer every build of `main`.

### Added
- **Offsite warning (3-2-1).** Arkive works out where each job's backups actually go and warns when no copy leaves the server: a local folder, or an SFTP or S3 endpoint that turns out to be this machine or one of its containers. Event: `backup.no_offsite`.
- **Per-database health.** The Databases page shows, for every database, when it was last dumped successfully, how large the dump was and whether anything is wrong, instead of one overall count.
- **Dump-shrink warning.** A dump at least 50% smaller than that database's recent dumps is flagged and the run becomes `partial`. The thresholds are under Settings → Monitoring.
- **Guided database restore** (Restore a database, on the Restore page). Pick a database and a snapshot; Arkive checks the dump, takes a safety dump of the current database, can stop the apps that use it, loads the dump and starts them again. A restore can be undone from the safety dump. PostgreSQL, MariaDB and MySQL are restored in place. SQLite files are restored, integrity-checked and left for you to swap in, with the exact steps, because Arkive does not write to your app data.
- **"Finish protecting this server" checklist** on the dashboard after setup: a clean first backup, a passed restore test, an offsite target, a notification channel and a saved recovery kit. Each step is judged from what actually happened, not from a checkbox.
- **Unraid Community Applications template**, with read-only data mounts by default.
- **User guide** in `docs/`: quick start, storage providers, restore, restore drills, monitoring, notifications, recovery kit, security, upgrading and troubleshooting.
- Keyboard and screen-reader support across the interface: a skip link, labelled controls and dialogs, visible focus, readable contrast in both themes, and layouts that work on a phone.
- A footer with the running version and links to the source.
- An automated upgrade test from 0.2.4 to the current version, and a restore round trip against real PostgreSQL, MariaDB, MySQL and SQLite, for people working on Arkive.

### Changed
- **Database dumps restore completely.** PostgreSQL dumps keep owners and grants. MariaDB and MySQL dumps include stored procedures, functions and events, and every application database on the server is dumped, not only `MYSQL_DATABASE`. MariaDB 11 images, which ship `mariadb-dump` instead of `mysqldump`, are supported.
- The `islamdiaa/arkive:latest` image now moves only when a new version is released. Until now every push to `main` republished `latest`, so the Unraid template and the example Compose file could pull unreleased code. Builds of `main` are still published as `main` and as the commit SHA.
- GitHub releases are created from this changelog when a version is tagged.
- A local storage target on a read-only mount is rejected when you add it, instead of failing at the first backup.
- The frontend moved to SvelteKit 3 and Tailwind CSS 4. Nothing should look or behave differently.
- Dependency updates: apprise 2.1.0, click 8.5.0, and the Inter and JetBrains Mono fonts.
- restic 0.19.1 and rclone 1.75.2 are rebuilt with `golang.org/x/net` 0.62.0, which fixes CVE-2026-78661.

### Fixed
- Restore-plan commands now run as written: they use the container's own database user and client, no longer rely on bash-only syntax, and handle SQLite write-ahead logs.
- Restore drills no longer fail on a `CREATE TABLE` inside a function body or on a MySQL system-schema dump.
- Log redaction no longer mangles container names such as `immich-postgres`.

### Known limitations
- MariaDB and MySQL user accounts and grants are not part of the dumps yet. After restoring onto a fresh server, re-create the application's database user.
- Guided restore does not cover MongoDB or Redis; restore those dumps by hand as described in the user guide.

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

[Unreleased]: https://github.com/islamdiaa/Arkive/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/islamdiaa/Arkive/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/islamdiaa/Arkive/compare/v0.2.4...v0.4.0
[0.2.4]: https://github.com/islamdiaa/Arkive/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/islamdiaa/Arkive/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/islamdiaa/Arkive/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/islamdiaa/Arkive/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/islamdiaa/Arkive/compare/a4c1be7cd3f44b64dca5a30e8dd50256ccfecdf9...v0.2.0
[0.1.0]: https://github.com/islamdiaa/Arkive/tree/a4c1be7cd3f44b64dca5a30e8dd50256ccfecdf9
