# Upgrading

Arkive keeps all of its state in `/config`. Upgrading means running a newer image with the same `/config` mapping. Database changes are applied automatically when the new version starts.

## Image tags

Images are published to Docker Hub as `islamdiaa/arkive`:

| Tag | Follows |
|-----|---------|
| `latest` | The newest stable release |
| `0` (major version) | The newest `0.x` release |
| `main` | Every change merged to the `main` branch |
| `0.6.0` (full version) | One release, never changes |
| `0.6` (minor version) | The newest `0.6.x` release |
| A commit SHA, such as `0f3f094` | One build of `main` |

`latest` follows releases, not the main branch. Security fixes go into the latest release (see [SECURITY.md](https://github.com/islamdiaa/Arkive/blob/main/SECURITY.md)). Release notes are on the [GitHub releases page](https://github.com/islamdiaa/Arkive/releases).

## Before you upgrade

1. **Make sure no backup, restore or drill is running.** When the container stops, Arkive waits up to 5 minutes for an active backup, but Docker only gives a container 10 seconds to stop by default. A run that is cut off is marked `failed` with "Interrupted by server restart" when Arkive starts again.
2. **Keep a copy of `/config`.** Migrations only go forward, so a copy is the way back to the older version. Stop the container, then copy the folder, including the hidden `.keyfile`:

    ```bash
    cp -a /mnt/user/appdata/arkive /mnt/user/appdata/arkive.before-upgrade
    ```

    You can delete the `cache` folder from the copy if it is large. restic rebuilds it.
3. **Have your recovery kit at hand.** If something goes wrong, it has the keyfile and the encryption password.

## Upgrade

- **Unraid:** on the Docker tab, apply the update for the Arkive container. To pin a version, edit the container and set **Repository** to `islamdiaa/arkive:0.4`.
- **Docker Compose:** `docker compose pull && docker compose up -d`.
- **docker run:** `docker pull islamdiaa/arkive:latest`, remove the container, and run it again with the same options.

## What happens when the new version starts

1. **Migrations** bring the database up to the new version. If one fails, Arkive doesn't start.
2. **The keyfile check** makes sure `/config/.keyfile` can decrypt the stored secrets. See [Troubleshooting](troubleshooting.md#arkive-refuses-to-start-keyfile) if it refuses to start.
3. **Clean-up** after the restart: runs, verifications and restores that were still marked running become `failed` ("Interrupted by server restart"), stale backup and restore locks are removed, leftover restore drill containers and scratch folders are deleted, and stale restic locks are removed from every enabled target.
4. **rclone configuration** is written again from the database for every enabled cloud or SFTP target.
5. **The scheduler starts**, and an "Arkive started" (`system.startup`) notification goes to the channels subscribed to it.

Afterwards, check the dashboard, then run a backup and a restore drill.

## If the upgrade fails

If the container stops (or keeps restarting) with `Migration N failed: ...` in its log (`docker logs Arkive`), Arkive rolled back all of that upgrade's migrations together. To return to a known state:

1. Stop the container.
2. Put back your copy of `/config`:

    ```bash
    rm -rf /mnt/user/appdata/arkive
    cp -a /mnt/user/appdata/arkive.before-upgrade /mnt/user/appdata/arkive
    ```

3. Run the version you had before (set its version tag as the image), and report the error on [GitHub](https://github.com/islamdiaa/Arkive/issues) with the log lines.

Going back to an older version on a database that a newer one has already migrated isn't supported. Use the copy you made before upgrading.

## Notes for older installs

Upgrading from a version before 0.4:

- **The keyfile check is new.** Before 0.4, a missing `/config/.keyfile` was silently replaced, which made every stored secret unreadable. Now Arkive refuses to start instead. Make sure the keyfile came along if you ever moved or restored `/config`.
- **New notification events.** Channels that were subscribed to `backup.failed` are also subscribed to `backup.partial`, `backup.stale`, `drill.failed`, `verification.failed` and `target.error` during the upgrade, so they keep hearing about every kind of problem. Partial runs, for example, used to be reported as failures.
- **Restore drills** are enabled by default (Sundays at 07:00). Turn them off or reschedule them under Settings → General → Restore Drills.
- **Dumps are uncompressed by default.** New dumps are plain `.sql` files, because uncompressed dumps deduplicate between snapshots and restic compresses the repository anyway. Existing `.gz` dumps stay restorable. Choose gzip under Settings → General → Database Dumps if the size of the local dump folder matters more.
- **Some older snapshots recorded the container ID as their host name.** The weekly retention job re-labels the snapshots this instance created with the real host name, so the old and new snapshots fall under one retention policy.
