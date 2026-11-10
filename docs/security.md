# Security and deployment model

Arkive needs broad access to do its job. It reads your app data, runs dump tools inside your database containers, and holds the credentials of your storage. Treat its web port like an admin console. This page expands on the deployment model in [SECURITY.md](https://github.com/islamdiaa/Arkive/blob/main/SECURITY.md), which is also where to report a vulnerability (privately, through GitHub Security Advisories, not public issues).

## What Arkive can reach

- **The Docker socket, which is root-equivalent.** It is mounted read-only, but read-only only stops the socket file itself from being changed: the Docker API behind it is fully usable. Arkive uses it to discover containers, to run dump tools such as `pg_dump`, `mysqldump`, `mongodump` and `redis-cli` inside your database containers, and to start throwaway database containers for [restore drills](restore-drills.md) (with no network, and removed afterwards). Anyone who controls Arkive can control Docker on the host.
- **Your data, read-only.** The Unraid template mounts `/mnt/user/appdata` and the flash configuration read-only. Arkive writes only to `/config`, `/cache`, the paths of local storage targets, and (during drills) Docker's own storage for the throwaway containers.
- **Root inside the container.** The template runs Arkive as root (`--user 0:0`) so it can read the flash configuration and app-owned SQLite files.

## How to deploy it

- **Keep port 8200 on your LAN, or behind an authenticating reverse proxy.** Don't expose it to the internet.
- **Finish setup right after installing.** Until the setup wizard has run, the only protected API routes that answer are the container and directory scans it needs; the rest return 401. But whoever completes setup first owns the instance, and until then `GET /api/status` returns the full status to anyone. Setup can be completed only once, and each IP address gets 5 attempts per 15 minutes.
- **Protect `/config`.** It holds Arkive's database, the keyfile that encrypts the stored secrets, `rclone.conf` with your storage credentials, the API key file, the unencrypted database dumps in `/config/dumps`, and anything you restore into `/config/restores`. On Unraid this is `/mnt/user/appdata/arkive`. Only the restic repositories are encrypted; the local dumps and restores are plain files.
- **Save the recovery kit somewhere other than the server.** See [Recovery kit](recovery-kit.md).
- **Behind a reverse proxy**, set `ARKIVE_TRUSTED_PROXIES` to the proxy's IP address or CIDR. Rate limiting then sees real client addresses (taken from `X-Forwarded-For`) instead of the proxy's, and the origin check for browser requests also accepts the proxy's `X-Forwarded-Host`. If the web interface is served from another origin, add it to `ARKIVE_CORS_ORIGINS`.

## What is in place

**Authentication**

- Before setup, the only protected routes that answer are the wizard's two scans. The setup call, the session check and `GET /api/status` are open.
- After setup, every API route needs the API key (`X-API-Key` header) or a browser session, except `GET /api/status`, the sign-in calls under `/api/auth` (session, login, logout and the now-closed setup), and the OpenAPI documentation at `/docs`. Unauthenticated `GET /api/status` returns only a minimal health payload (overall status and version), for Docker health checks. The OpenAPI documentation describes the API but returns no data.
- The API key (`ark_` followed by 64 hex characters) is stored only as a SHA-256 hash. It is shown once, at the end of setup or after rotation, and written to `/config/.api_key` with mode 0600 for the CLI.
- Browser sessions are encrypted `HttpOnly`, `SameSite=Strict` cookies valid for 30 days and bound to the current API key, so rotating the key signs every other browser out. State-changing requests made with a session cookie must come from the same origin (or a configured CORS origin).
- After 5 failed logins or wrong API keys within 5 minutes, that IP address is locked out for 60 seconds.
- Live updates in the browser use single-use tokens that expire after 60 seconds, not the session cookie.

**Secrets**

- The restic password, storage credentials and notification URLs are encrypted at rest in Arkive's database with `/config/.keyfile` (Fernet).
- Secrets are redacted from API responses, logs and error messages.
- `rclone.conf` is written atomically with mode 0600. SFTP passwords in it are obscured with `rclone obscure`, which hides them from casual reading but is not encryption.
- Arkive refuses to start when the keyfile is missing, corrupt or doesn't match the database, instead of silently creating a new key (see [Troubleshooting](troubleshooting.md#arkive-refuses-to-start-keyfile)).

**Commands and input**

- External tools (restic, rclone, the dump tools) are run without a shell, with arguments passed as lists. Restore drills run fixed scripts inside their throwaway containers, with the variable parts passed as environment variables.
- User-supplied values that end up on a restic or rclone command line go after `--` or in `--flag=value` form, and paths and snapshot IDs that start with `-` or contain control characters are rejected, so they can't be read as options.
- Storage target values are checked for control characters before they reach `rclone.conf`, so a value can't add lines to it.
- Local target paths and restore destinations can't point into system folders. Restores can only go to Arkive's restore folders, so the data you protect can stay mounted read-only.
- Request bodies over 1 MB are rejected.

**Browser**

- Responses carry a Content Security Policy that limits scripts, styles, images, fonts and connections to Arkive's own origin and forbids framing, plus `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff` and a strict referrer policy.

**Build and release**

- CI runs bandit on every pull request, and a finding of medium or high severity fails the build. `pip-audit` and `npm audit` run too, but only report: their findings don't fail the build.
- Images built from version tags are scanned with Trivy after they have been pushed to Docker Hub. A fixable HIGH or CRITICAL finding fails the release build, but it doesn't withdraw the image, so a published version tag is not proof that the scan passed.
- restic and rclone are built from their release tags (checked against the release commits) with a current Go toolchain, rather than downloaded as prebuilt binaries.
