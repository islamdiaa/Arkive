# Restore round trip (real engines)

`restore_roundtrip.py` checks that what Arkive backs up can actually be put
back, using real database servers and a real Arkive instance:

1. starts a throwaway Docker-in-Docker daemon (nothing on the host is
   discovered, dumped, or swept by the Arkive under test)
2. starts the fixtures: Postgres 16 and 17, MariaDB 11.4 (with and without
   `MARIADB_DATABASE`), MySQL 8.4, and an app that holds SQLite databases open
   in WAL mode
3. sets Arkive up through its API and runs the `full` backup job
4. restores `/config/dumps` from the snapshot through the restore API
5. follows the database steps of the restore plan Arkive generates
   (`restore-plan.html`) into fresh servers, then compares data, owners,
   grants, roles, extensions, large objects, encodings, routines, events and
   the second database of each server with the source
6. runs a restore drill and expects every database to pass

Each check prints `PASS`/`FAIL`; the exit status is 0 only when all passed.

```sh
docker build -t arkive-rt:dev .
python3 scripts/restore-roundtrip/restore_roundtrip.py --image arkive-rt:dev
# test backend changes without rebuilding the image:
python3 scripts/restore-roundtrip/restore_roundtrip.py --image arkive-rt:dev --live-code
# keep the environment, rerun against it, remove it:
python3 scripts/restore-roundtrip/restore_roundtrip.py --image arkive-rt:dev --live-code --keep
python3 scripts/restore-roundtrip/restore_roundtrip.py --image arkive-rt:dev --reuse --keep
python3 scripts/restore-roundtrip/restore_roundtrip.py --down
```

Requirements: Python 3.9+, a Docker host that can run a privileged
`docker:dind` container, and network access to pull the fixture images
(about 2 GB; `--load-local-images` copies them from the host instead). A run
takes 10-20 minutes. Everything it creates is named after `--prefix`
(default `arkive-rt`) and lives in `--workdir` (default `/tmp/<prefix>`); the
API listens on `127.0.0.1:--port` (default 8320).

It is not part of CI: it needs a privileged container and pulls several
database images. The problems it found are covered by unit tests
(`backend/tests/unit/test_restore_roundtrip_findings.py` and the
"Real-engine findings" section of `test_restore_drill.py`).
