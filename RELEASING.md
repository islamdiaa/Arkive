# Releasing Arkive

A release is a `vX.Y.Z` tag on `main` (or, for a fix to an older line, on a
`release/` branch; see [below](#fixes-for-an-older-release-line)). Pushing
the tag runs the CI workflow (`.github/workflows/build.yml`), which:

1. checks that `CHANGELOG.md` has an `[X.Y.Z]` section and that the version files
   say `X.Y.Z` (lint job), then runs the full test suite;
2. builds the image and pushes `islamdiaa/arkive:X.Y.Z` and `X.Y`, plus `X` when
   this is the newest stable release of major version X, and `latest` when it
   is the newest stable release overall;
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
      leaves a published image without a release. Scan locally first, with
      the Trivy version CI uses (trivy-action v0.36.0 runs Trivy 0.70.0),
      pinned by digest, and without giving it the Docker socket: it scans a
      saved copy of the image.
      ```bash
      docker build -t arkive:release-check .
      docker save -o /tmp/arkive-release-check.tar arkive:release-check
      docker run --rm -v /tmp/arkive-release-check.tar:/image.tar:ro \
        aquasec/trivy:0.70.0@sha256:be1190afcb28352bfddc4ddeb71470835d16462af68d310f9f4bca710961a41e \
        image --input /image.tar --ignore-unfixed --severity HIGH,CRITICAL --exit-code 1
      rm /tmp/arkive-release-check.tar
      ```
      When the trivy-action pin in `build.yml` changes, move this pin to the
      Trivy version it runs (its `version` input default) and that tag's
      digest (`docker buildx imagetools inspect aquasec/trivy:<version>`).
      Don't run a scanner here by a moving tag such as `latest`: this is
      usually run on the machine that holds the backups.
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

When the workflow finishes, check the release page and the tags on Docker Hub:
`latest` and `X` should point at the newest stable release.

- **Lint or tests failed** (nothing was pushed): delete the tag
  (`git push --delete origin vX.Y.Z && git tag -d vX.Y.Z`), fix `main`, tag again.
- **"Compare this tag with the existing version tags" failed** (for example
  `git ls-remote` hit a network error): nothing was pushed. Re-run the failed
  jobs. The step fails rather than guess, because a tag compared against an
  empty list would look like the newest release and take `latest`.
- **The image was pushed** (Trivy or a later step failed): don't move the tag.
  Ship the fix as the next patch version.
- **Only the release job failed:** re-run that job.
- **A GitHub release for the tag already exists** (for example one written by
  hand): the workflow leaves it unchanged.

Pre-release tags such as `vX.Y.Z-rc.1` publish only the `X.Y.Z-rc.1` image tag
and a GitHub pre-release. The notes come from an `[X.Y.Z-rc.1]` section if there
is one, otherwise from `[Unreleased]`, and the version files are not compared
with the tag.

## Fixes for an older release line

GitHub runs the workflow from the tagged commit, not from `main`. What this
file describes (only the newest stable release moves `latest` and `X`, the
`CHANGELOG.md` check, the GitHub release) therefore happens only for commits
that contain the release tooling: `scripts/release.py`, `CHANGELOG.md` and the
`build.yml` that calls them. Tags up to and including v0.4.0 predate it. Their
`build.yml` keeps docker/metadata-action's default `latest=auto`, which moves
`latest` and `X` to every stable tag, so a fix tagged on a branch cut from one
of them would move `latest` backwards (v0.4.1 replacing v0.5.0) and create no
release.

To ship vX.Y.(Z+1) after a newer version is out:

1. Branch from the release you are fixing, named after it as `release/v0.2.0`
   was: `git checkout -b release/vX.Y.Z vX.Y.Z`. CI runs on `release/**`.
2. If that release predates the release tooling, bring it in before anything
   else: cherry-pick the commit on `main` that added it
   (`git log --diff-filter=A --format=%h main -- scripts/release.py`), or copy
   `scripts/release.py`, `CHANGELOG.md` and the release steps of
   `.github/workflows/build.yml`.
   Then confirm the branch has it (both lines must print):
   ```bash
   grep -n 'latest=false' .github/workflows/build.yml
   grep -n 'release.py tag-info' .github/workflows/build.yml
   ```
3. Cherry-pick the fix, add a `## [X.Y.(Z+1)] - YYYY-MM-DD` section and its link
   to `CHANGELOG.md`, set the version files, and run
   `python scripts/release.py check --tag vX.Y.(Z+1)`.
4. Push the branch, wait for CI to pass on it, then tag the branch head and
   push the tag. Work through the checklist above as for any release.
5. Add the same section and link to `CHANGELOG.md` on `main`, in version order
   (below newer releases).
