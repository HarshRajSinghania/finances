# PikaPods

PikaPods hosts open source apps as managed "pods". They don't take packages or
pull requests: apps are added by the PikaPods team, chosen from suggestions and
votes on their feedback board.

- Where: <https://feedback.pikapods.com/> (linked from
  <https://docs.pikapods.com/faq/apps>). Post a new app suggestion; votes from
  users raise its priority, so link the post from the README or a release note
  once it is up.
- Open source authors can be offered a share of the revenue from their app's
  pods (third-party sources and app READMEs mention 10 to 15%). To ask about
  that, get in touch with PikaPods directly after posting; the request below
  mentions it.

## What they look for, and where finances stands

From their FAQ, an app should roughly:

| Requirement | finances |
|---|---|
| Be a web app using one HTTPS port | Yes: one HTTP port, 8787, which they put behind HTTPS |
| Not use heavy CPU or bandwidth | Yes: parsing a statement takes well under a second; idle otherwise |
| No real abuse risk to other pods (proxies, VPNs) | Yes |
| License that permits self-hosting | MIT |
| Not compete with the author's own paid hosting | There is none |
| Official or semi-official Docker image | `ghcr.io/roshanlam/finances` (multi-arch, built by `.github/workflows/docker.yml`) |
| Actively developed, security issues handled | Yes; `SECURITY.md` in the repo |

**The blocker to fix before asking:** a pod is reachable on the public internet
at `<name>.pikapod.net`, and finances has no login. Today anyone who found the
URL could read every transaction. Before this is worth suggesting, finances
needs its own authentication for the dashboard and API (even a single
password set from an env var such as `FINANCES_PASSWORD`, with a session
cookie), and PikaPods needs to set `FINANCES_ALLOWED_HOSTS` to the pod's
hostname. Don't post the suggestion until that ships, or say plainly in it that
it is coming.

What a pod needs from the image, so the request can state it:

- Port: `8787`
- Volume: `/data` (store, rules, reports; statements in `/data/PDFs`)
- Env: `FINANCES_ALLOWED_HOSTS=<pod hostname>`; optional `FINANCES_CURRENCY`,
  `FINANCES_LLM_ENDPOINT`; the login variable once it exists
- Runs as UID 1000, no extra capabilities, about 100 MB RAM

## Request text (for feedback.pikapods.com)

**Title:** finances: personal finance from your own bank statements, no bank login

**Body:**

> finances (https://github.com/roshanlam/finances, MIT) turns the statement
> PDFs and exports people already download from their bank (PDF, CSV,
> OFX/QFX, CAMT.053, MT940, plus Mint/YNAB/Monarch/Copilot/Tiller exports)
> into a spending dashboard: categories, subscriptions and price rises, cash-flow
> forecast, budgets, year in review. It never asks for a bank password or uses
> an aggregator like Plaid.
>
> What makes it different: each statement prints its own totals, so after
> extracting every transaction finances checks the sum against the printed
> balances and reports any statement that doesn't reconcile, to the cent.
>
> It seems a good fit for PikaPods because the whole point is keeping financial
> data off third-party aggregators, and many people who want that don't want to
> run a server. It is a single container:
>
> - Image: `ghcr.io/roshanlam/finances` (amd64 and arm64, version tags)
> - One HTTP port: 8787
> - One volume: `/data`
> - Env: `FINANCES_ALLOWED_HOSTS` set to the pod's hostname; built-in login via
>   `FINANCES_PASSWORD` [update to match what ships]
> - Light: SQLite, about 100 MB RAM, no background jobs
>
> I'm the author and happy to adjust the image for your platform, and would be
> glad to talk about the revenue share for open source authors.
>
> Try it locally first with made-up data:
> `uvx finances demo && uvx finances web --db demo.db --open`
