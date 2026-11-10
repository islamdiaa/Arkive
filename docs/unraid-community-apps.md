# Listing Arkive in Unraid Community Applications

This page is for the maintainer. It lists what the repository already provides for a
Community Applications (CA) listing, and the steps that have to be done by hand to get
Arkive listed. Nothing has been submitted yet.

Checked against the CA submission portal's help pages, the official starter repository and
the Unraid Docker manager source in November 2026 (links at the end). CA's rules change;
re-read the portal's help before submitting.

## What the repository provides

| File | Purpose |
|------|---------|
| `unraid-template.xml` | The Docker template CA lists and Unraid installs from. `TemplateURL` points at its raw URL on `main`. |
| `ca_profile.xml` | Repository profile (overview, icon, web page, support link). The portal blocks submission without it, or with an empty `<Profile>`. |
| `docs/images/arkive-icon.png` | 256×256 PNG icon, used by the template and the profile. Rendered from `frontend/static/icons/logo.svg`. |
| `LICENSE` | MIT, which is OSI-approved, as the portal requires. |

`backend/tests/unit/test_unraid_template.py` checks both XML files: required fields,
links into this repository, valid CA categories, the icon, the image tag and the mount modes.

### Template decisions

- **Image tag `islamdiaa/arkive:0`.** `latest` follows the newest stable release, including the next
  major version, so a template on `:latest` would move users to 1.0 without a template
  change. The `0` tag is published only by `v0.*` release tags (`type=semver,pattern={{major}}`
  in `.github/workflows/build.yml`). It always points at the newest `0.x` release.
  docker/metadata-action's docs suggest turning that pattern off for 0.x versions. Keep it on
  while the template uses `:0`; the template test fails if it gets a condition.
- **Read-only data.** `/mnt/user/appdata`, `/boot/config` (as `/boot-config`) and the Docker
  socket are mounted `ro`. Arkive writes only to `/config` (restores land in
  `/config/restores`), `/cache` and `/mnt/user/backups`, a read-write path for a local
  target. A local target on a read-only mount (inside appdata, say) is now rejected when
  it's created, with a message saying why; 0.4.0 accepted it and restic failed later.
- **Shares are mapped one at a time, not `/mnt/user` as a whole.** With "Permit exclusive
  shares" on (Unraid 6.12+), a share that lives only on a pool is a symlink in `/mnt/user`
  (`/mnt/user/appdata -> /mnt/cache/appdata`). A container that only has `/mnt/user`
  sees a symlink to a path it doesn't have, so appdata would be invisible: not preselected
  in setup, not suggested by the directory scan, and no SQLite databases discovered, with
  no warning. Mapping appdata on its own as well doesn't fix that: Docker mounts it at
  `/mnt/cache/appdata` inside the container, `/mnt/user/appdata` stays a symlink, and
  restic stores the symlink, not the data. A direct mapping (`/mnt/user/appdata` to
  `/mnt/user/appdata`) is resolved on the host and works whether or not the share is
  exclusive. Users add other shares the same way: same host and container path, read-only.
  Mappings also don't nest: a read-write path inside a read-only `/mnt/user` can only be
  created if that folder already exists in `/mnt/user` on the host, so pointing "Local
  Backups" at an Unassigned Devices disk would stop the container from starting.
  `docker-compose.yml` still mounts `/mnt/user` whole, so it has the exclusive-share
  problem; that's outside this template.
- **No `TZ` variable.** Unraid's Docker manager passes the server's time zone to every
  container. The old template's `TZ=America/New_York` came later on the command line and
  replaced it.
- **PNG icon.** Unraid's Docker tab shows a question mark for SVG icons, and most CA apps
  use PNG.
- **Category `Backup: Tools:System`.** Both are on CA's category list.
- **`--restart unless-stopped --user 0:0`.** Root is needed to read the flash config and
  app-owned SQLite files. The restart policy brings Arkive back after a crash; containers
  stopped from the Docker tab stay stopped.
- **Support** is GitHub issues. CA accepts that, but its popup labels the link "Support
  Forum", and Unraid's docs ask developers to answer support requests in their forum
  threads (optional step 5).

## Steps to do by hand

### 1. Merge to `main`

`TemplateURL`, `Icon` and the screenshots are raw `main` URLs, and the portal scans the default branch.

### 2. Check the raw URLs

```bash
for f in unraid-template.xml ca_profile.xml docs/images/arkive-icon.png; do
  curl -s -o /dev/null -w "%{http_code} $f\n" "https://raw.githubusercontent.com/islamdiaa/Arkive/main/$f"
done
```

All three must print `200`.

### 3. Check the image tag

```bash
docker buildx imagetools inspect islamdiaa/arkive:0     | grep -m1 Digest
docker buildx imagetools inspect islamdiaa/arkive:0.5.0 | grep -m1 Digest
```

Both digests must match the newest release. Only a plain `vX.Y.Z` tag moves `0`;
pre-release tags such as `v0.5.0-rc1` don't.

### 4. Test-install the template

Use a test server or a VM with an Unraid trial licence, with no Arkive installed. Don't
test on a server whose Arkive install you rely on. The template's defaults are the
production install's: the container name `Arkive`, the config folder
`/mnt/user/appdata/arkive` (its database and `.keyfile`) and port 8200. When you click
**Apply**, Unraid saves the form as `my-<container name>.xml`, matched without regard to
case, so a test container named `Arkive` overwrites an existing `my-arkive.xml`, whatever
name you downloaded the template under. A second Arkive on the same config folder shares
the first one's database and keyfile, and deleting "the test install's appdata" afterwards
deletes the real one.

```bash
wget -O /boot/config/plugins/dockerMan/templates-user/my-Arkive-test.xml \
  https://raw.githubusercontent.com/islamdiaa/Arkive/main/unraid-template.xml
```

Go to **Docker → Add Container → Template → Arkive-test**. If the server already has an
Arkive container, despite the advice above, change these before you apply:

| Field | Change to |
|-------|-----------|
| Name | `Arkive-test` |
| Web UI Port | a free port, such as `8201` |
| Config Directory | `/mnt/user/appdata/arkive-test` |
| Restic Cache (Show more settings) | `/mnt/user/appdata/arkive-test/cache` |
| Local Backups | a new, empty folder, such as `/mnt/user/arkive-test-backups` |

Apply, then check that:

- the command Unraid prints contains `-v '/mnt/user/appdata':'/mnt/user/appdata':'ro'`,
  `-v '/boot/config':'/boot-config':'ro'`, `-v '/var/run/docker.sock':'/var/run/docker.sock':'ro'`
  and only one `TZ` (the server's), and no `-v '/mnt/user':'/mnt/user'`
- the Docker tab shows the Arkive icon, and **WebUI** opens `http://<server>:<port>`
- the setup wizard preselects `/mnt/user/appdata` and accepts the local target
  `/mnt/user/backups`. A path such as `/mnt/user/photos/arkive` is rejected as "not
  persistent storage". From the first release after this change, `/mnt/user/appdata/arkive-repo`
  is rejected as "mounted read-only"; 0.4.0 accepts it, and the target then fails when
  restic tries to write to it.
- if the server has **Settings → Global Share Settings → Permit exclusive shares** on and
  appdata is exclusive (`ls -l /mnt/user` shows `appdata` as a symlink), discovery still
  lists the SQLite databases of your apps
- a manual backup finishes `success`, flash config included, and the container's health
  shows `healthy`

Afterwards, remove the test container from the Docker tab, delete `my-Arkive-test.xml` from
`/boot/config/plugins/dockerMan/templates-user/`, and delete only the folders you created
for the test. Check each path before you delete it: never `/mnt/user/appdata/arkive`.

### 5. Optional: create a forum support thread

Post a support thread in the Unraid forums' **Docker Containers** board. Put its URL in
`<Support>` in `unraid-template.xml` and `<Forum>` in `ca_profile.xml`, and merge that first.

### 6. Submit

1. Open <https://ca.unraid.net/submit> and choose **Start Submission**. Sign in when asked. I
   couldn't see which account it asks for; CA repository entries link to the author's Unraid
   forum profile, so have one ready.
2. Enter `https://github.com/islamdiaa/Arkive`.
3. Run **Validate and scan**. Expect one Docker app (Arkive) from `unraid-template.xml`, the
   profile from `ca_profile.xml`, the MIT licence, and no duplicate: no CA app used the
   `islamdiaa/arkive` image as of 2026-11-09.
   The help pages don't say where templates must live. The starter keeps them in
   `templates/`, but at least one app added in November 2026 keeps `unraid-template.xml`
   at the root of its app repository. If the scan doesn't find the template, move it with
   `git mv unraid-template.xml templates/arkive.xml` and update `TemplateURL`, the README
   link and `TEMPLATE_PATH` in the test. Don't keep a copy in both places; CA rejects two
   templates with the same name in one repository.
4. Fix anything the scan reports, merge, and scan again.
5. Fill in the listing details (the template's `<Overview>` works as the description) and
   submit for review.

Approved apps appear after CA's next catalog build.

### 7. After approval

- On a test server, search **Apps** for "Arkive". Check the icon, the Backup category, that
  the install shows the "additional requirements" text from `<Requires>`, and that it
  installs `islamdiaa/arkive:0`.
- Change the README's Unraid section to point at Community Applications first.

## Keeping the listing healthy

- **At 1.0.0**, change `<Repository>` to `islamdiaa/arkive:1`, but only after the `v1.0.0`
  tag's build has published the image (`docker buildx imagetools inspect islamdiaa/arkive:1`
  succeeds). Not in the release-prep PR that bumps `ARG VERSION`: until the final tag is
  built, `:1` doesn't exist and every new install would fail to pull, and pre-release tags
  such as `v1.0.0-rc1` never publish it. The template test allows the template's major to
  lag `ARG VERSION` by one and fails if it gets ahead. Containers installed with `:0` stay
  on the last 0.x release until their owners edit the repository field, so say so in the
  release notes.
- **Template edits** reach CA on its next catalog build. Installed containers keep their
  own copy of the template on the flash drive, so new or changed paths only reach new
  installs, or users who edit the container.
- **Don't rename or transfer** the GitHub repository or the Docker Hub image without
  telling the CA moderators. CA may remove an app first and ask questions later.
- **Never put commands in `ExtraParams` or `PostArgs`.** CA treats extra commands as a
  security violation and blacklists the whole repository. Keep the icon static: animated
  icons aren't allowed.

## Sources

- CA submission portal: <https://ca.unraid.net/submit> and its help pages:
  [repository XML](https://ca.unraid.net/submit/help/repository-xml),
  [ca_profile.xml](https://ca.unraid.net/submit/help/repository-info-xml),
  [XML field reference](https://ca.unraid.net/submit/help/xml-field-reference),
  [builder guide](https://ca.unraid.net/submit/help/builders)
- Starter repository: <https://github.com/unraid/unraid-community-apps-starter>
- CA application policies: <https://forums.unraid.net/topic/87144-ca-application-policies-notes/>
- CA category list: `categoryList.json` in <https://github.com/Squidly271/AppFeed>
- Unraid Docker manager (`xmlToCommand`, which builds the `docker create` command):
  `emhttp/plugins/dynamix.docker.manager/include/Helpers.php` in <https://github.com/unraid/webgui>
- SVG icons in the Docker tab:
  <https://forums.unraid.net/bug-reports/stable-releases/custom-docker-icons-by-path-w-svg-or-webp-icons-unexpected-behavior-r3755/>
