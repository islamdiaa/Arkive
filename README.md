<h1 align="center">Arkive</h1>

<p align="center">
  <strong>Open-source backup and disaster recovery for Unraid servers.</strong><br />
  Discover Docker workloads, capture application-aware database dumps, encrypt every snapshot,
  and restore with confidence.
</p>

<p align="center">
  <a href="https://github.com/islamdiaa/Arkive/actions/workflows/build.yml"><img src="https://github.com/islamdiaa/Arkive/actions/workflows/build.yml/badge.svg" alt="CI" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/islamdiaa/Arkive" alt="License" /></a>
  <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python" />
  <img src="https://img.shields.io/badge/platform-amd64%20%7C%20arm64-lightgrey" alt="Platform" />
</p>

---

## About Arkive

Most backup tools make you configure every container, database, and directory by hand. Arkive does not.

Arkive is an open-source backup and recovery system built for Unraid. It auto-discovers Docker containers and their databases, creates application-aware dumps, stores encrypted restic snapshots, and supports cloud or local storage targets from a single interface.

## Status

Arkive is under active development. Core backup, snapshot, restore, and cloud-target workflows are implemented, with unit, integration, and end-to-end coverage in the repository.

---

## Features

### Core Backup Engine
- **Zero-config container discovery** — Automatically detects running containers with 50+ built-in app profiles (Nextcloud, Immich, Plex, Home Assistant, Vaultwarden, and more)
- **Application-aware database dumps** — PostgreSQL, MariaDB/MySQL, MongoDB, SQLite, Redis, InfluxDB with per-engine integrity verification
- **Encrypted, deduplicated backups** — Powered by [restic](https://restic.net/) with AES-256-CTR encryption and content-defined chunking
- **Multi-target cloud sync** — Backblaze B2, AWS S3, SFTP, or local storage via [rclone](https://rclone.org/)
- **Flash drive backup** — Backs up your Unraid USB flash configuration automatically

### Operations & Reliability
- **Flexible scheduling** — Cron-based schedules for DB dumps, cloud sync, and flash backups with APScheduler (no system cron dependency)
- **Retention policies** — Configurable keep-daily, keep-weekly, keep-monthly, keep-yearly per storage target
- **Notifications** — Discord, Slack, and [ntfy](https://ntfy.sh/) alerts for backup success, failure, and system events
- **Activity log** — Full audit trail of every backup run, discovery, and configuration change
- **Health monitoring** — Continuous system health checks with status reporting

### Restore & Recovery
- **One-click restore plan** — Downloadable PDF with step-by-step disaster recovery instructions tailored to your configuration
- **Granular restore** — Browse snapshots, select specific files or databases to restore
- **Snapshot browser** — Explore all restic snapshots across storage targets with filtering

### Interface
- **Beautiful dark-mode dashboard** — Real-time backup progress, system health, discovered containers, storage usage
- **6-step setup wizard** — Guided onboarding: encryption, storage, schedules, directories, notifications
- **Full settings management** — General, security, storage targets, jobs, schedule, directories, notifications
- **API-first design** — Complete REST API with OpenAPI documentation at `/docs`

---

## Quick Start

### Docker Compose (recommended)

```yaml
services:
  arkive:
    image: ghcr.io/islamdiaa/arkive:latest
    container_name: arkive
    # On Unraid, run as root for flash backup and app-owned SQLite host reads.
    user: "0:0"
    ports:
      - "8200:8200"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /mnt/user/appdata/arkive:/config
      - /mnt/user/appdata:/mnt/user/appdata:ro
      - /boot/config:/boot-config:ro
    environment:
      - TZ=America/New_York
    restart: unless-stopped
```

```bash
docker compose up -d
```

Open `http://your-server:8200`. On a new install, Arkive shows the setup wizard. After setup is complete, the browser UI may redirect to `/login` and ask for the admin API key once to create a browser session.

Ensure the host path mounted at `/config` is writable by the container runtime before first boot. If you add explicit `/data` or `/cache` mounts, make those writable too.

On Unraid, run Arkive as `root` for full coverage. Flash backup (`/boot/config`) and some app-owned SQLite files are not readable to non-root UIDs, so use `user: "0:0"` in Compose or `--user 0:0` with `docker run`.

After deployment, you can run the homeserver smoke test in [docs/homeserver-smoke-test.md](docs/homeserver-smoke-test.md).

### Unraid Community Applications

Search for **Arkive** in the [Community Applications](https://unraid.net/community/apps) store, or install manually:

```
https://raw.githubusercontent.com/islamdiaa/Arkive/main/unraid-template.xml
```

---

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│                    Arkive Pipeline                       │
│                                                         │
│  1. DISCOVER     Docker socket → detect containers      │
│                  Match against 50+ app profiles         │
│                                                         │
│  2. DUMP         Application-aware database dumps       │
│                  pg_dump, mysqldump, mongodump, etc.    │
│                                                         │
│  3. SNAPSHOT     restic backup → encrypted, deduped     │
│                  AES-256-CTR + content-defined chunks   │
│                                                         │
│  4. SYNC         rclone → B2, S3, SFTP, local           │
│                  Multi-target with parallel uploads      │
│                                                         │
│  5. RETAIN       Configurable retention policies         │
│                  keep-daily, weekly, monthly, yearly     │
│                                                         │
│  6. NOTIFY       Discord, Slack, ntfy                    │
│                  Per-event filtering + throttling        │
└─────────────────────────────────────────────────────────┘
```

---

## Supported Databases

| Engine | Dump Method | Streaming | Profile Examples |
|--------|-------------|-----------|------------------|
| PostgreSQL | `pg_dump` | Yes | Nextcloud, Immich, Gitea, Authentik |
| MariaDB / MySQL | `mysqldump` | Yes | WordPress, Bookstack, Firefly III |
| MongoDB | `mongodump` | Yes | Unifi Controller, Rocket.Chat |
| SQLite | File copy | Yes | Vaultwarden, Home Assistant, Paperless |
| Redis | `BGSAVE` + dump.rdb | No | Authelia, Outline |
| InfluxDB | `influx backup` | Yes | Grafana, Telegraf |

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `UTC` | Timezone for schedules and logs |
| `ARKIVE_CONFIG_DIR` | `/config` | Path to config directory (SQLite DB, logs) |
| `ARKIVE_DEV_MODE` | `0` | Set to `1` to skip Docker/binary validation |

### Volumes

| Mount | Purpose | Required |
|-------|---------|----------|
| `/var/run/docker.sock:/var/run/docker.sock:ro` | Container discovery | Yes |
| `/config` | Database, logs, cache | Yes |
| `/mnt/user/appdata:/mnt/user/appdata:ro` | App data to back up | Recommended |
| `/boot/config:/boot-config:ro` | Unraid flash config backup | Optional |

Mount any additional host paths you plan to protect as read-only volumes. The quick-start example only mounts `/mnt/user/appdata`.

For Unraid, full host coverage assumes the container runs as `root`. Running as a non-root UID is still supported, but expect `partial` backups when flash backup or host SQLite reads are denied.

### API Authentication

Browser UI authentication uses an `HttpOnly` session cookie created during setup or by `POST /api/auth/login`. CLI clients and external integrations continue to use the `X-API-Key` header. `GET /api/status` is intentionally public for health checks.

```bash
curl -H "X-API-Key: ark_your_key_here" http://localhost:8200/api/jobs
```

Full API documentation is available at `http://your-server:8200/docs` (OpenAPI/Swagger).

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| **Backend** | Python 3.12, FastAPI, SQLite (WAL mode), APScheduler |
| **Frontend** | SvelteKit 2.x, Svelte 5, Tailwind CSS |
| **Backup** | restic (encrypted, deduplicated), rclone (cloud sync) |
| **Container** | Docker multi-stage build (Node + Python), root runtime recommended on Unraid for host access |
| **CI/CD** | GitHub Actions, multi-arch (amd64 + arm64), GHCR |
| **Security** | AES-256-CTR encryption, SHA-256 hashed API keys, Fernet-encrypted secrets |
| **Testing** | pytest (unit + integration), Playwright (E2E), Vitest (component) |

---

## Development

### Prerequisites

- Python 3.12+ with `pip`
- Node.js 20+ with `npm`
- Docker (for container discovery features)

### Setup

```bash
# Clone the repository
git clone https://github.com/islamdiaa/Arkive.git
cd Arkive

# Backend
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Frontend
cd ../frontend
npm install
```

### Running Locally

```bash
# Terminal 1: Backend
cd backend
ARKIVE_DEV_MODE=1 ARKIVE_CONFIG_DIR=/tmp/arkive-dev \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8200 --reload

# Terminal 2: Frontend
cd frontend
npm run dev
```

Open `http://localhost:5173` — the frontend proxies API requests to port 8200.

### Testing

```bash
# Backend unit + integration tests
cd backend && .venv/bin/python -m pytest tests/ -q --tb=short

# Frontend type checking
cd frontend && npx svelte-check

# E2E tests (starts backend automatically)
cd frontend && npx playwright test

# Single E2E spec
npx playwright test tests/e2e/dashboard.spec.ts
```

### Project Structure

```
arkive/
├── backend/                  Python 3.12 + FastAPI
│   ├── app/
│   │   ├── api/              REST API routes
│   │   ├── core/             Security, config, dependencies
│   │   ├── models/           Pydantic request/response models
│   │   ├── services/         Business logic (orchestrator, scheduler, etc.)
│   │   └── main.py           Application entrypoint
│   └── tests/                pytest unit + integration tests
├── frontend/                 SvelteKit 2.x + Tailwind
│   ├── src/
│   │   ├── lib/              Components, stores, utilities
│   │   └── routes/           SvelteKit file-based routing
│   └── tests/e2e/            Playwright E2E tests
├── profiles/                 50+ YAML container profiles
├── Dockerfile                Multi-stage build (Node + Python)
├── docker-compose.yml        Production deployment
└── docs/                     Documentation + screenshots
```

---

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

1. Fork the repository
2. Create your feature branch (`git checkout -b feat/amazing-feature`)
3. Run the test suite (`pytest` + `npx playwright test`)
4. Commit your changes
5. Open a Pull Request

---

## Roadmap

- [ ] Automated backup verification (test restore)
- [ ] Prometheus metrics endpoint (`/api/metrics`)
- [ ] Multi-user RBAC with role-based access
- [ ] Database migration framework
- [ ] Scheduled email digest reports

See [docs/homeserver-smoke-test.md](docs/homeserver-smoke-test.md) for additional deployment validation guidance.

## Security

Please do not commit credentials, internal infrastructure details, or production configuration to the repository.

If you discover a security vulnerability, do not open a public issue. Use GitHub Security Advisories or contact the maintainers privately.

---

## License

[MIT](LICENSE)

---

<p align="center">
  <sub>Built for the Unraid community. Backups should be automatic, encrypted, and boring.</sub>
</p>
