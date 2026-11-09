# Troubleshooting

## Where to look

- **Backups page.** Every run with its status. A `partial` or `failed` run shows its reason under the row. Open a run to see each target's and each database's result with the error text.
- **Activity.** Discovery scans, retention, target health changes, overdue jobs, drills and restores.
- **Logs page**, or `/config/logs/arkive.log` (`/mnt/user/appdata/arkive/logs/arkive.log` on the host). Logs are JSON lines, rotated at 20 MB with 10 files kept. `docker logs Arkive` shows the same output plus anything printed before Arkive could open its log file.
- **`GET /api/status`** with your API key. Its `checks` section reports the database, the scheduler, disk space on `/config` and the restic and rclone binaries.

## A run ends as `partial`

`partial` means a snapshot was written to at least one target but something was missed. The run's message, `Backup completed with problems (...)` or `Backup completed with failures (...)`, lists every problem, separated by semicolons. A run that is `partial` every time usually means configuration drift, such as a watched directory that no longer exists or a target that was disabled.

| Message | What it means | What to do |
|---------|---------------|------------|
| `N database dump(s) failed` | A dump tool exited with an error, or its output was cut short (a dump without its completion trailer counts as failed and is deleted; the previous good dump is kept). | Open the run and read the database's error. Common causes are credentials missing from the database container's environment, or a container that restarted during the dump. |
| `roles dump failed for <container>` | `pg_dumpall --globals-only` failed for a PostgreSQL container. The databases themselves may be fine. | Read the error in the run details. |
| `flash backup: ...` | `tar` couldn't read or archive the flash configuration. | Run the container as root (`--user 0:0`), and check free space in `/config`. |
| `container discovery failed, databases were not dumped (...)` | The Docker daemon didn't answer, after 3 attempts 20 seconds apart. | Check Docker and the socket mapping. The next run retries. |
| `no databases discovered (a recent run found N)` | Discovery worked but found no databases, while a run of this job in the last 30 days did. | Check whether the database containers were removed or renamed. If that was intentional, the warning stops 30 days after the last run that found databases. |
| `database(s) in stopped containers have never been dumped: ...` | A database container is stopped, and there is no earlier dump of it. | Start the container so a dump can be made, or exclude the database on the Databases page. Stopped containers that already have a dump are skipped without a warning. |
| `backup path(s) not found: ...` | A watched directory doesn't exist inside the container. restic would otherwise drop it silently. | Map the path into the container, or remove it under Settings → Directories. |
| `<target>: some files could not be read (...)` | restic wrote the snapshot but couldn't read some files (permission denied, or files that disappeared mid-backup). | Run the container as root. To leave out a folder that can't or needn't be read, put an empty file named `.arkive-exclude` in it; restic skips every folder that contains one, and every folder marked as a cache with a `CACHEDIR.TAG` file. |
| `no enabled storage targets, nothing was uploaded` | The job had nowhere to upload. | Add or enable a target. |
| `Backup completed with failures (<target>: ...)` | Some targets failed and at least one succeeded. | See [A storage target fails](#a-storage-target-fails). |

Databases you excluded on the Databases page are recorded as `skipped` and don't make a run partial.

## Flash backups are missing

Arkive recognises an Unraid server by its flash configuration mapped at `/boot-config`. If `/boot/config` isn't mapped there, Arkive treats the host as plain Linux and skips the flash backup without a warning, and `GET /api/status` reports `platform: "linux"`. Map `/boot/config` to `/boot-config` read-only (the Unraid template does) and restart the container.

## A run ends as `failed`

| Message | What it means | What to do |
|---------|---------------|------------|
| `Backup completed with failures (...)` and no target succeeded | Every upload failed. | See [A storage target fails](#a-storage-target-fails). |
| `Insufficient disk space on dump directory (/config/dumps): ...` | Less than 1 GB is free where the dumps are written. Below 5 GB, Arkive logs a warning but carries on. | Free space on the share that holds `/mnt/user/appdata/arkive`. The limits are the `min_disk_space_bytes` and `warn_disk_space_bytes` settings, which can be changed through the API. |
| `Interrupted by server restart` | The container stopped during the run. | Nothing, unless it keeps happening. Docker gives a container 10 seconds to stop by default, which isn't enough for a running backup. |
| `Restore operation in progress` | A manual run was started during a restore. | Wait for the restore to finish. |

A manual run started while another backup is running is normally refused before a run is recorded: **Backup Now** shows "Another backup is already running". A scheduled run that finds another backup or a restore running waits up to 3 hours. If it still can't start, it is recorded as `skipped` with the reason.

## A storage target fails

- Click **Test** on the target under Settings → Storage Targets. The message is the error from rclone or the filesystem, with credentials removed.
- **Authentication errors** (`unauthorized`, `forbidden`, `SignatureDoesNotMatch`, `AccessDenied`): wrong or expired keys or token. Dropbox tokens without a refresh token expire; see [Dropbox](storage-providers.md#dropbox).
- **Network errors** are tried up to 3 times during a backup before the target counts as failed. Check that the container can reach the provider.
- **`repository is already locked`**: another restic process (prune, check, a drill, or a restic run outside Arkive) holds the lock. Arkive waits up to 10 minutes for a lock, and removes stale locks on every enabled target when it starts.
- **Wrong password**: if the repository was created with a different encryption password, restic can't open it. A target pointing at a repository from another installation must use that installation's password.

## A job is overdue

The dashboard shows "A backup job is overdue" and you get a **Backups overdue** notification. Work through:

1. **Is the job running at all?** Look for recent runs on the Backups page. If there are none, check that the job is enabled and its schedule is right (Settings → Schedule shows the next run), and that `checks.scheduler` in `GET /api/status` is ok.
2. **Are the runs good?** Runs that `failed`, or `partial` runs that didn't write a snapshot or (for database jobs) dumped no database, don't count. Fix the reason shown on the run.
3. **Is the window too short?** If a job legitimately runs less often than its schedule suggests, set **Overdue after (hours)** under Settings → Monitoring.
4. **Is the time zone right?** Schedules follow the container's `TZ` variable.

The dashboard alert clears as soon as the job has a good run again, and the heartbeat resumes at the next 15-minute check. See [Overdue backups and heartbeat](monitoring.md) for the exact rules.

## The heartbeat monitor alerts but nothing is overdue

The heartbeat is withheld whenever any job's latest finished run is bad, not only when a job is overdue. Check the Backups page for a `failed` run, or a `partial` run that wrote no snapshot or dumped no database. It is also withheld when no job is enabled.

If backups are fine, use **Send test ping** under Settings → Monitoring. If the test fails, the URL is wrong or the container can't reach the monitor. The log records each failed ping as `Heartbeat ping failed: ...`.

## Arkive refuses to start (keyfile)

Stored secrets (the encryption password, storage credentials, notification URLs) are encrypted with `/config/.keyfile`. When the keyfile is missing, corrupt, or can't decrypt any of them, Arkive refuses to start instead of silently creating a new key that would strand them. The container log shows one of:

- `Refusing to start: /config/.keyfile is missing, but the database holds N values encrypted with it ...`
- `Refusing to start: /config/.keyfile is corrupt (...). Restore it from your recovery kit.`
- `Refusing to start: /config/.keyfile does not match this database: none of its N stored secrets can be decrypted with it ...`

**Fix: put the original keyfile back.** It is in your [recovery kit](recovery-kit.md) under *Config keyfile*. Write it to `/mnt/user/appdata/arkive/.keyfile` and start the container. This usually happens after `/config` was restored from a copy that didn't include the hidden `.keyfile`, or after the appdata folder was moved without it.

**If the keyfile is gone for good**, the secrets in the database can't be recovered. Your backups are still fine as long as you have the encryption password. Start over with a fresh database:

1. Stop the container, and move `arkive.db`, `arkive.db-wal`, `arkive.db-shm` and `.keyfile` out of `/mnt/user/appdata/arkive/`.
2. Start the container and run the [setup wizard](setup-wizard.md) with the same encryption password and the same storage target settings. The existing repositories and snapshots are picked up.

This loses Arkive's history and settings (jobs, channels, run history). You can bring them back from a snapshot together with the keyfile only if you still have that keyfile; see [Bring back Arkive's own settings](restore.md#bring-back-arkives-own-settings).

`ARKIVE_ALLOW_NEW_KEYFILE=1` makes Arkive start anyway with a new key. Every stored secret stays unreadable. Storage credentials and notification URLs can be entered again, but the encryption password can't be changed after setup, so backups can't run on that database. Starting over as described above is the better option.

## The container exits at startup

- `Docker socket required. Mount with -v /var/run/docker.sock:/var/run/docker.sock:ro`: map the Docker socket.
- `Refusing to start: ...`: see [the keyfile section](#arkive-refuses-to-start-keyfile).
- `Migration N failed: ...`: a database upgrade failed. See [Upgrading](upgrading.md#if-the-upgrade-fails).

## Can't sign in

- **Lost the API key?** It is in `/mnt/user/appdata/arkive/.api_key` on the host.
- **"Too many failed attempts. Try again later."** After 5 wrong keys within 5 minutes, that IP address is locked out for 60 seconds.
- **Behind a reverse proxy**, every client shares the proxy's IP address for that limit. Set `ARKIVE_TRUSTED_PROXIES` to the proxy's address so Arkive uses the client address from `X-Forwarded-For`.

## A restore fails

- `Cannot restore while a backup is running` or `Another restore operation is already running`: wait and try again.
- `Cannot restore to ...: ... is mounted read-only`: restore to a folder under `/config/restores`.
- `restore_to must be inside a safe restore root`: the destination must be inside `/config/restores`, `/tmp/arkive-restore` or `/var/tmp/arkive-restore`. See [Restoring backups](restore.md).
