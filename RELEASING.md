# Releasing Arkive

A release is a `vX.Y.Z` tag on `main`. Pushing the tag runs the CI workflow
(`.github/workflows/build.yml`), which:

1. checks that `CHANGELOG.md` has an `[X.Y.Z]` section and that the version files
   say `X.Y.Z` (lint job), then runs the full test suite;
2. builds the image and pushes `islamdiaa/arkive:X.Y.Z` and `X.Y`, plus `X` and
   `latest` when this is the newest stable release;
3. scans the pushed image with Trivy and fails on fixable HIGH/CRITICAL findings;
4. creates the GitHub release `Arkive X.Y.Z` with the `[X.Y.Z]` section of
   `CHANGELOG.md` as its notes.

Pushes to `main` publish only the `main` and commit-SHA image tags. `latest` is
always a release.

## Checklist

Before tagging (the rules in `CLAUDE.md` section 14):

- [ ] **Changelog.** Rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a
      new empty `## [Unreleased]` above it, and update the links at the bottom.
      Write it for people upgrading: what changed for them, and anything they
      have to do.
- [ ] **Version.** Set `X.Y.Z` in `backend/pyproject.toml`, `backend/app/__init__.py`,
      `frontend/package.json` and `frontend/package-lock.json`
      (`cd frontend && npm version X.Y.Z --no-git-tag-version`), and `ARG VERSION`
      in the `Dockerfile`. Then `python scripts/release.py check --tag vX.Y.Z`
      must pass.
- [ ] **Dependencies.** Run `pip-audit -r backend/requirements.txt` and
      `cd frontend && npm audit`; fix anything fixable that ships in the image.
- [ ] **Image scan.** Trivy runs after the image is pushed, so a finding there
      leaves a published image without a release. Scan locally first:
      ```bash
      docker build -t arkive:release-check .
      docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy \
        image --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1 arkive:release-check
      ```
- [ ] **restic/rclone arguments.** No user-supplied value sits in an argv
      position where it can be parsed as an option: it goes after `--` or as
      `--flag=value`. Review new `restic`/`rclone` calls since the last tag.
- [ ] **Setup bypass.** Only routes the setup wizard needs are in `_SETUP_ROUTES`
      (`backend/app/core/dependencies.py`); everything else returns 401 before setup.
- [ ] **Light theme.** Every token in `:root` in `frontend/src/app.css` has a value
      under `.light`.
- [ ] **Public-repo hygiene.** No real hostnames, IPs, emails or secrets in the
      changes since the last tag (run gitleaks, or grep the diff).
- [ ] **CI is green on `main`** for the commit you are tagging.

## Tagging

```bash
git checkout main && git pull
git tag -a vX.Y.Z -m "Arkive X.Y.Z"
git push origin vX.Y.Z
```

When the workflow finishes, check the release page and the tags on Docker Hub.

- **Lint or tests failed** (nothing was pushed): delete the tag
  (`git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`), fix `main`, tag again.
- **The image was pushed** (Trivy or a later step failed): don't move the tag.
  Ship the fix as the next patch version.
- **Only the release job failed:** re-run that job.
- **A GitHub release for the tag already exists** (for example one written by
  hand): the workflow leaves it unchanged.

Pre-release tags such as `vX.Y.Z-rc.1` publish only the `X.Y.Z-rc.1` image tag
and a GitHub pre-release. The notes come from an `[X.Y.Z-rc.1]` section if there
is one, otherwise from `[Unreleased]`, and the version files are not compared
with the tag.
