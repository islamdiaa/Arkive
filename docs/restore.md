# Restoring backups

Arkive never restores over your live data. A restore writes the files into a separate folder, and you copy what you need back into place. That keeps a restore from overwriting something by accident, and it lets Arkive mount the data it protects read-only.

The commands on this page run in the Unraid terminal. They assume the container is called `Arkive` (the name the Unraid template gives it) and that you have your API key in a variable:

```bash
ARKIVE_API_KEY=$(cat /mnt/user/appdata/arkive/.api_key)
```

## Before you start

- **Where restores go.** A restore destination must be inside `/config/restores`, `/tmp/arkive-restore` or `/var/tmp/arkive-restore`. Use `/config/restores/<name>`, which is `/mnt/user/appdata/arkive/restores/<name>` on the host. The two `/tmp` locations are inside the container: you can't see those files from the host, and they disappear when the container is re-created.
- **Files keep their full original path** under the destination. Restoring a snapshot to `/config/restores/today` puts your app data in `/mnt/user/appdata/arkive/restores/today/mnt/user/appdata/` and the database dumps in `.../today/config/dumps/`.
- **Make room.** A full restore needs about as much free space as the data in the snapshot.
- **One at a time.** Only one restore runs at a time, and a restore can't start while a backup is running ("Cannot restore while a backup is running"). Scheduled backups wait for a running restore to finish.
- **Read-only destinations are refused.** If the destination is on a read-only mount, Arkive says so before restoring anything.
- **Clean up afterwards.** Arkive leaves `/config/restores` out of its own backups, but the restored files take space until you delete them.

## Restore a snapshot in the web interface

1. Open **Snapshots**. Snapshots are listed newest first, with their short ID, host name, age, storage target ID, size and tags. The tags `job:<id>` and `run:<id>` tell you which job and run made each one. Use the target filter if you have several targets.
2. **Browse** shows the top level of a snapshot.
3. Click **Restore** on the snapshot you want.
4. Change **Restore To** from the pre-filled `/tmp/arkive-restore` to a folder under `/config/restores`, for example `/config/restores/today`.
5. **Dry run** is ticked by default. **Preview Restore** then lists what the snapshot contains and writes nothing, which checks that the snapshot can be read.
6. Untick **Dry run** and click **Restore Now**. The dialog restores everything in the snapshot.

The snapshot list is a cache. Arkive refreshes it after every backup run for the targets the run uploaded to. To reload it from the repositories now, for example on a fresh install:

```bash
curl -X POST -H "X-API-Key: $ARKIVE_API_KEY" http://localhost:8200/api/snapshots/refresh
```

## Restore only some paths

The web interface restores whole snapshots. To restore part of one, list paths through the API. `paths` takes paths as they appear in the snapshot, and an empty list means everything. Without `restore_to`, the files go to a new folder `/config/restores/restore-<id>`.

```bash
curl -X POST -H "X-API-Key: $ARKIVE_API_KEY" -H "Content-Type: application/json" \
  -d '{"snapshot_id": "<snapshot-id>", "target": "<target-id>",
       "paths": ["/config/dumps"], "restore_to": "/config/restores/dumps"}' \
  http://localhost:8200/api/restore
```

The CLI inside the container does the same:

```bash
docker exec Arkive python -m app.cli --api-key "$ARKIVE_API_KEY" restore list
docker exec Arkive python -m app.cli --api-key "$ARKIVE_API_KEY" restore run <snapshot-id> \
  --target <target-id> --paths /mnt/user/appdata/vaultwarden --restore-to /config/restores/vaultwarden
```

`restore list` and the Snapshots page show each snapshot's ID and target ID.

## Copy files back into place

Arkive's mounts of your data are read-only, so you copy restored files back from the Unraid terminal:

1. Stop the app's container.
2. Copy its folder back, for example:

    ```bash
    rsync -a /mnt/user/appdata/arkive/restores/today/mnt/user/appdata/vaultwarden/ /mnt/user/appdata/vaultwarden/
    ```

    Add `--delete` if files created after the snapshot should be removed.
3. Start the container.
4. Delete the restore folder when you are done.

restic restores file owners and permissions when it runs as root, which it does with the template's `--user 0:0`.

Restoring an app's folder brings back its database files as they were on disk during the backup, possibly in the middle of a write. For an app with a database, also restore the database from its dump (next section).

## Restore a database from its dump

Dumps are in the `config/dumps/` folder of a restore. Each file is named `<container>_<database>-<hash>_<YYYYMMDD_HHMMSS>.<ext>`. `arkive-dumps-manifest.json` in the same folder lists every dump with its container, image, database type, database name and, for SQLite, the path where the database file was found. Arkive keeps only the newest dump of each database in the dump folder, so a snapshot normally holds one dump per database. Older dumps are in older snapshots.

Stop the apps that use a database before you restore it.

### PostgreSQL (`.sql`, or `.sql.gz` with gzip compression)

Arkive dumps each database with `pg_dump --clean --create --if-exists --no-owner --no-privileges`. Loading the file drops the database if it exists, creates it again and fills it, so connect to the `postgres` maintenance database:

```bash
docker exec -i <postgres-container> psql -U <user> -d postgres -v ON_ERROR_STOP=1 < <dump>.sql
gunzip -c <dump>.sql.gz | docker exec -i <postgres-container> psql -U <user> -d postgres -v ON_ERROR_STOP=1
```

`<user>` is the database superuser of that container, usually its `POSTGRES_USER`. A [restore drill](restore-drills.md) loads dumps the same way. Because the dump has no owner or privilege statements, the restored objects belong to the user you load it with.

On a new database server, load the roles first. Arkive dumps them per PostgreSQL container with `pg_dumpall --globals-only` into `<container>_globals-<hash>_<timestamp>.sql`. Load that file the same way, without `ON_ERROR_STOP` (the role you connect as already exists, and creating it again reports an error you can ignore). If the dump user couldn't read role passwords (it wasn't a superuser), Arkive made the roles dump with `--no-role-passwords`, and you need to set the passwords again.

### MariaDB and MySQL (`.sql`)

Arkive dumps each database with `mysqldump --single-transaction --databases <name>` (`mariadb-dump` on MariaDB images). The file creates the database and switches to it, so load it without naming a database:

```bash
docker exec -i <mariadb-container> mysql -u root -p'<root-password>' < <dump>.sql
```

On images without a `mysql` command, use `mariadb`.

### MongoDB (`.archive`, or `.archive.gz`)

Arkive uses `mongodump --archive`. Restore with `mongorestore`. `--drop` replaces existing collections. Add `--gzip` for `.archive.gz`, and `--username`, `--password` and `--authenticationDatabase admin` if the server needs a login:

```bash
docker exec -i <mongo-container> mongorestore --archive --drop < <dump>.archive
```

### SQLite (`.sqlite3`)

The dump is a complete copy of the database made with SQLite's online backup, checked with `PRAGMA integrity_check`. To restore it, stop the app, copy the file over the app's database file (the manifest's `host_path` says where that is) under the original name, delete any `-wal` and `-shm` files next to it, and start the app.

### Redis (`.rdb`)

The dump is Redis's own `dump.rdb`, written by `BGSAVE`. Stop the Redis container, put the file in its data folder as `dump.rdb`, and start it. If the server uses append-only persistence, Redis loads the append-only file instead, so move that aside first.

## Restore the Unraid flash configuration

Flash backups are `flash_<timestamp>.tar` files in the dump folder: a tar archive of the contents of `/boot/config`, uncompressed. Extract it to a working folder and copy what you need, such as `super.dat`, `shares/`, `plugins/` or the Docker templates in `plugins/dockerMan/templates-user/`, onto the flash drive's `config` folder:

```bash
mkdir /tmp/flash && tar -xf flash_<timestamp>.tar -C /tmp/flash
```

Arkive keeps the 7 newest flash archives in the dump folder.

## Disaster recovery on a fresh server

When the server is gone, you need:

- the **encryption password**, from your [recovery kit](recovery-kit.md),
- the **repository location** of at least one target, also in the kit, and
- the **credentials** for that storage (B2 key, S3 keys, SFTP password and so on), from your provider account or password manager.

### With a fresh Arkive install

1. **Install Unraid and Arkive.** Set up the array and shares, then install Arkive as in the [quick start](quick-start.md), with the same path mappings. For a local target, bring the repository folder back first: mount the disk, or copy the folder to the same path.
2. **Run the setup wizard** with the **same encryption password** and the **same storage settings**: the same bucket for B2, S3 and Wasabi, the same remote path for SFTP, the same path for a local target. Arkive finds the existing repository and uses it as it is.
3. **Pause every job straight away**, under Settings → Schedule or Settings → Backup Jobs. A backup of the half-restored server would become the newest snapshot, and the retention that runs after each backup could then forget older snapshots from the same day or week.
4. **Load the snapshot list:** `curl -X POST -H "X-API-Key: $ARKIVE_API_KEY" http://localhost:8200/api/snapshots/refresh`. The API key is on the wizard's last page and in `/mnt/user/appdata/arkive/.api_key`.
5. **Restore the newest good snapshot** into `/config/restores/dr` from the Snapshots page, as described above. If the newest one was taken after things started going wrong, pick an older one.
6. **Restore the flash configuration** if you are rebuilding the flash drive, from `config/dumps/flash_<timestamp>.tar` in the restore.
7. **Copy app data back**, with the app containers stopped. Leave Arkive's own folder out, because Arkive is using it (the leading `/` limits the exclusion to the top-level `arkive` folder):

    ```bash
    rsync -a --exclude '/arkive/' /mnt/user/appdata/arkive/restores/dr/mnt/user/appdata/ /mnt/user/appdata/
    ```

8. **Recreate your containers.** Their Unraid templates are in the flash archive under `plugins/dockerMan/templates-user/`.
9. **Restore the databases** from their dumps, as described above, before you start the apps that use them.
10. **Resume the jobs,** run a backup, and run a restore drill.
11. **Generate a new recovery kit.** The targets were created again, so their IDs and repository strings changed.

### Bring back Arkive's own settings

Every snapshot contains `config/dumps/arkive_self.db`, a copy of Arkive's database (jobs, targets, notification channels, settings and history). The secrets in it are encrypted with the old instance's keyfile, which is in your recovery kit. To bring it back once your apps are running again:

1. Stop the Arkive container.
2. In `/mnt/user/appdata/arkive/`, replace `arkive.db` with the restored `arkive_self.db`, and delete `arkive.db-wal` and `arkive.db-shm` if they exist.
3. Write the keyfile from the recovery kit to `/mnt/user/appdata/arkive/.keyfile`, with mode 600.
4. Start Arkive. If the keyfile doesn't match the database, Arkive refuses to start and says so (see [Troubleshooting](troubleshooting.md#arkive-refuses-to-start-keyfile)).
5. Sign in with the **old** instance's API key. If `/mnt/user/appdata` was a watched directory, it is in the restore at `mnt/user/appdata/arkive/.api_key`.

The jobs resume on their schedules as soon as Arkive starts.

### With restic alone

You don't need Arkive to read the backups. Install [restic](https://restic.net/) on any machine.

**Local repository.** Copy the repository folder (the kit's host path) to the machine, or mount the disk, then:

```bash
export RESTIC_PASSWORD='<encryption password>'
restic -r /path/to/arkive-repo snapshots
restic -r /path/to/arkive-repo restore latest --target /restore
```

**Cloud or SFTP repository.** Arkive reaches these through rclone, and the repository string has the form `rclone:<remote>:<path>`. Install [rclone](https://rclone.org/), run `rclone config`, and create a remote whose name is the `<remote>` part of the string, with the same storage type and your credentials. Then use the repository string as it is:

```bash
restic -r rclone:<remote>:<bucket>/arkive-backups snapshots
restic -r rclone:<remote>:<bucket>/arkive-backups restore latest --target /restore
```

To restore part of a snapshot, add `--include`, for example `--include /config/dumps` for just the database dumps. If more than one server has used the repository, add `--host <name>` to pick the latest snapshot of the right one.
