# CasaOS / ZimaOS app

`docker-compose.yml` is a CasaOS App Store definition: standard Compose plus a
top-level `x-casaos` block for the store listing, following the v2 docs in
[IceWhaleTech/CasaOS-AppStore](https://github.com/IceWhaleTech/CasaOS-AppStore)
(`docs/specs/compose-and-x-casaos.md`) and the layout of existing apps such as
`Apps/ActualBudget`.

- Dashboard on host port `8797` (8787 is used by another app in the store).
- Data in `/DATA/AppData/$AppID/data` (store, rules, reports) and
  `/DATA/AppData/$AppID/PDFs` (statements, also where dashboard uploads land).
- `FINANCES_ALLOWED_HOSTS` defaults to `casaos.local,zimaos.local`. CasaOS has
  no variable for the device's IP, so the install tip tells people opening it
  by IP to add theirs in the app settings.
- Runs as root inside the container, because CasaOS creates the AppData folders
  owned by root and the image's default user (UID 1000) could not write to them.
- CasaOS puts no login in front of apps and finances has none, so the install
  tip says plainly that anyone on the network can read the data.

## Before submitting

1. Release a version that includes `FINANCES_ALLOWED_HOSTS` and let the docker
   workflow push `ghcr.io/roshanlam/finances:<version>` (amd64 and arm64).
   Make the ghcr package public.
2. Set the image tag and `x-casaos.version` to it (both say `0.5.0` now; 0.4.1
   predates the env var). Update `update_at`.
3. Prepare assets: `icon.svg` (the dashboard's `web/public/favicon.svg` works,
   ideally on a square background) and `screenshot-1.png` to `screenshot-3.png`
   of the dashboard running on demo data (`finances demo`), never real data.

## Submitting to the official store

It is a pull request to
[IceWhaleTech/CasaOS-AppStore](https://github.com/IceWhaleTech/CasaOS-AppStore).

```bash
gh repo fork IceWhaleTech/CasaOS-AppStore --clone && cd CasaOS-AppStore
mkdir -p Apps/Finances
cp ../finances/packaging/casaos/docker-compose.yml Apps/Finances/
cp icon.svg screenshot-1.png screenshot-2.png screenshot-3.png Apps/Finances/
docker compose -f Apps/Finances/docker-compose.yml config -q     # must pass
./scripts/build_dist.sh                                           # must finish; writes dist/apps/io.github.roshanlam.finances/
```

Test it on a CasaOS or ZimaOS machine: App Store, Custom Install, Import, paste
the compose file; open it, run through an upload, restart, and check the data
is kept. Then open a PR titled "Add finances" saying it is a new app, what it
does, and how you validated it (the build script output and the install test).
The repo's CI validates the compose file on the PR.

The `icon` and `screenshot_link` URLs point at `Apps/Finances/` on the store's
`main` branch through jsDelivr, the same pattern the other apps use, so they
resolve once the PR is merged.

## Without the official store

Anyone can install it today: CasaOS App Store, Custom Install, Import, and
paste `docker-compose.yml`. A third-party store is also possible (see the
store repo's `docs/guides/third-party-store-guide.md`), but that means hosting
a built `dist/` and asking users to add its URL, which is more than one app needs.
