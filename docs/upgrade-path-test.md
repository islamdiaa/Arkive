# Upgrade Path Test

`scripts/upgrade-path-test.py` checks that an install made by an older release
keeps working when its container is re-created from an image built from this
tree, the way Unraid users upgrade: same `/config`, same backup directory, new
image.

```bash
make test-upgrade                                  # build this tree, upgrade from 0.2.4
make test-upgrade ARGS="--new-image arkive:dev"    # use an image you already built
make test-upgrade ARGS="--keep"                    # leave everything for debugging
scripts/upgrade-path-test.py --help
```

Needs Docker and Python 3.9+ (standard library only). Run it before tagging a
release, and after changes to migrations (`backend/app/core/database.py`), the
keyfile guard, settings or secret storage, dump naming, job semantics, or how
restic is invoked. It is not a CI job (yet): it needs an image built from the
tree (restic and rclone compile from source) and either a privileged daemon or
a Docker host of its own, and it has only been run locally so far. On a busy
host most of the time goes into copying the images into the private daemon.

## What it does

1. Starts the old release (default `islamdiaa/arkive:0.2.4`, `--old-image` to
   change) as root, as the Unraid template runs it, with a sample directory
   mounted read-only, a backup volume, and a throwaway Postgres container
   holding a marker row.
2. Completes setup through the old release's own API with a local target,
   adds a notification channel with events the old UI offered, runs the full
   backup job, and records the snapshot, the dump file, the `.keyfile` hash and
   the schema version.
3. Stops it and starts the new image on the same volumes.
4. Checks that the new image:
   - starts, with the keyfile guard accepting the old `.keyfile` (unchanged)
   - applied every schema migration in its code, including the data
     migrations (the old channel gains the new failure events)
   - accepts the old API key, keeps the run history, doesn't re-enter setup
   - lists the old snapshot from its cache and after a refresh from the repo
   - restores files from the old snapshot byte-for-byte
   - restores the old database dump, which then loads into a fresh Postgres
     with the original rows
   - passes a restore drill on the old snapshot (written before dump manifests
     existed, so the drill infers its dumps)
   - runs a new backup with status `success` that dumps the database, writes a
     new snapshot and keeps the old one; that dump restores with a row written
     after the upgrade
   - runs the other jobs the old setup created (`db_dump`, `flash`) with
     status `success`, and reports `status: ok`
   - refuses to start when the `.keyfile` is missing (so the guard really
     checks the old database), and starts again once it is back

## Isolation

Arkive needs the Docker socket, and 0.2.4 discovers and dumps every database
it can see. By default the test therefore runs everything inside a private,
privileged `docker:dind` daemon that holds only the test containers, so it is
safe on a machine with real containers. The images are copied into that
daemon, which can take several minutes on a busy host.

`--docker host` uses the current daemon instead (faster, no privileged
container). It refuses to run when that daemon has any container the test
didn't create, so it is only for throwaway machines such as CI runners.

Test data lives in named Docker volumes, not host directories. Everything the
test creates is named with `--name-prefix` (default `arkive-upgrade-test-`)
and removed at the end, including the image it built (`--new-image` tests an
existing image and leaves it alone). The API is published on
`127.0.0.1:8310` (`--port`).

## When it fails

The failing check is printed with `FAIL:`, followed by the last log lines of
the old and new Arkive containers. Rerun with `--keep` to inspect the
containers and volumes; afterwards remove them with
`docker rm -f <prefix>dind && docker volume rm <prefix>dind-lib` (dind mode),
or the `<prefix>*` containers and volumes (host mode).
