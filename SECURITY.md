# Security Policy

## Supported versions

Security fixes go into the latest release. Upgrade by pulling the newest version tag
(for example `islamdiaa/arkive:0.4`, which moves to each new 0.4.x release) and recreating
the container. [Upgrading](docs/upgrading.md) lists what each image tag follows.

| Version | Supported |
|---------|-----------|
| 0.4.x   | Yes       |
| < 0.4   | No        |

## Reporting a vulnerability

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/islamdiaa/Arkive/security/advisories/new),
not through public issues or pull requests. Include the version, how to reproduce the
problem, and what an attacker could do with it.

You should get a response within a week. Once a fix is released, the advisory is published
with credit to the reporter, unless you prefer to stay anonymous.

## Deployment model

Arkive needs broad access to do its job, so treat its web port like an admin console:

- **The Docker socket is root-equivalent.** Arkive mounts it read-only to discover
  containers and run database dump tools inside them. Anyone who controls Arkive can
  control Docker on the host.
- **Keep the port on your LAN or behind an authenticating reverse proxy.** Don't expose
  port 8200 to the internet.
- **Finish setup right after installing.** Until the setup wizard has run, the only
  protected API routes that answer are the container and directory scans it needs; the
  rest return 401. Setup can be completed only once, and whoever completes it first owns
  the instance.
- **Protect `/config`.** It holds the database, the keyfile that encrypts stored
  secrets, and local restores. Save the recovery kit somewhere other than the server.

## What is in place

- Stored secrets (restic password, storage credentials) are encrypted at rest with
  `/config/.keyfile`, and redacted from API responses, logs and error messages.
- API keys are checked against a stored hash. Browser sessions use `HttpOnly`,
  `SameSite=Strict` cookies with origin checks, and repeated failed logins are rate-limited.
- External tools are run without a shell, and user-supplied values are validated and
  passed so they can't be read as command-line options.
- Restores only write to Arkive's own restore folders (`/config/restores`, or
  `/tmp/arkive-restore` and `/var/tmp/arkive-restore` inside the container), so backed-up
  data can stay mounted read-only.
- CI runs bandit on every pull request, and release images are scanned with Trivy after
  they are pushed.
