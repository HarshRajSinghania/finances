# Umbrel app

An umbrelOS app package for finances: `umbrel-app.yml` (the manifest),
`docker-compose.yml`, and `data/PDFs/.gitkeep` (Umbrel needs every bind-mount
source directory committed). It follows the current packaging guide in
[getumbrel/umbrel-apps](https://github.com/getumbrel/umbrel-apps)
(`.agents/skills/umbrel-package-app/SKILL.md` there is the source of truth).

How it fits together:

- Manifest `port: 8797` is what users open (`http://umbrel.local:8797`). Umbrel's
  `app_proxy` forwards it to the container's `8787`. 8787 and 8788 are already
  taken by other Umbrel apps; 8797 was free when this was written, so re-check
  it before submitting (the linter reports conflicts).
- Umbrel's own login protects the app (the `app_proxy` default). finances has no
  login, so this must stay on; the description says so up front.
- Everything persists in `${APP_DATA_DIR}/data`, mounted at `/data`: the SQLite
  store, learned rules, reports, and `data/PDFs` for statements. The image runs
  as UID 1000, which is what Umbrel expects.
- `FINANCES_ALLOWED_HOSTS` lets the dashboard answer at the Umbrel's names
  (`${DEVICE_DOMAIN_NAME}`, `${DEVICE_HOSTNAME}`) and the container name. The
  server otherwise refuses any Host that is not localhost. People who open the
  app by IP add it in the app's settings (umbrelOS 2.0 `environment:` entry).
- On umbrelOS 2.0 the `folderAccess` entry lets users point the app at a folder
  of statements in their Umbrel files.

## Before submitting

1. Release a version that includes `FINANCES_ALLOWED_HOSTS`, and let
   `.github/workflows/docker.yml` push `ghcr.io/roshanlam/finances:<version>`
   for amd64 and arm64. Make the ghcr package public.
2. Set `version:` in `umbrel-app.yml` and the image tag in `docker-compose.yml`
   to that release (both say `0.5.0` now; 0.4.1 predates the env var and would
   answer every request with 403).
3. Pin the digest: `image: ghcr.io/roshanlam/finances:<version>@sha256:<digest>`.
   The workflow's run summary prints the exact line; or
   `docker buildx imagetools inspect ghcr.io/roshanlam/finances:<version>`.

## Option A: the official Umbrel App Store

It is a pull request to [getumbrel/umbrel-apps](https://github.com/getumbrel/umbrel-apps).

```bash
gh repo fork getumbrel/umbrel-apps --clone && cd umbrel-apps
mkdir finances
cp -R ../finances/packaging/umbrel/{umbrel-app.yml,docker-compose.yml,data} finances/
npm ci
npm run lint:apps -- finances --check-images
git diff --check
```

Then test it on a real Umbrel (or umbrelOS in a VM): install it, open it,
upload a statement or run the demo, restart the app, and confirm the data is
still there. Their `umbrel-test-app` skill describes exactly what reviewers
expect.

Open the PR with: app name and version, the upstream repo, the image source
(`ghcr.io/roshanlam/finances`, built by this repo's workflow), the testing you
did, the note that the app has no login of its own and relies on Umbrel auth,
and 3 to 5 screenshots plus the logo in the PR body (do not commit them; the
Umbrel team makes the gallery and icon). After opening it, set `submission:` in
the manifest to the PR URL and push.

## Option B: a community app store (no review, available today)

A community store is a public GitHub repo users add in umbrelOS
(App Store, the "..." menu, Community App Stores, paste the repo URL). Start
from the template at
[getumbrel/umbrel-community-app-store](https://github.com/getumbrel/umbrel-community-app-store).

1. Create the repo from the template, for example `roshanlam/umbrel-apps`.
2. In `umbrel-app-store.yml` set `id` (a short prefix, for example `roshanlam`)
   and `name`.
3. App IDs in a community store must start with the store's id, so copy this
   package into `roshanlam-finances/` and change, to match:
   - `id: roshanlam-finances` in `umbrel-app.yml`
   - `APP_HOST: roshanlam-finances_web_1` in `docker-compose.yml`
   - `roshanlam-finances_web_1` at the end of the `FINANCES_ALLOWED_HOSTS` default
4. Community stores may host their own images: add `icon:` (a URL) to the
   manifest and fill `gallery:` with image URLs (see the comment in the manifest).
5. Push. Users add `https://github.com/roshanlam/umbrel-apps` as a community
   store and install from there.

## Things worth checking on a real device

- Whether the dashboard opens at `umbrel.local:8797`, by IP once that is added
  to the setting, and over HTTPS on umbrelOS 2.0. If the proxy rewrites the
  Host or Origin header to something not in the list, the API returns
  `requests are accepted from localhost only`.
- That `FINANCES_ALLOWED_HOSTS` is also honoured for the `Origin` header on
  uploads, since the browser sends `Origin: http://umbrel.local:8797`.
