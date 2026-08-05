# NovaFabric Showcase Site

> This is a **showcase site explaining what NovaFabric does**. It is
> documentation, not a viewer. The canonical interface is the CLI
> (`pip install novafabric`). When `nova serve` ships in v1.x, that will
> be the local browser viewer; this site will remain explanatory.
>
> The site consumes **only baked-in fixture data** — it does not read
> user capsules, does not access the registry, and does not phone home.
> If it ever does any of those things, it has crossed the
> [non-goals](../docs/architecture.md#what-novafabric-is-not) line and must be removed
> or moved.

## What this is

A static, interactive marketing site that explains NovaFabric's five
primitives — Asset Registry, Run Capsule, Replay, Lineage, Evidence
Bundle — using a single coherent demo scenario (the
"code-review-agent v0.1.0 → v0.2.0 regression" story).

The goal: a first-time visitor goes from *"what is this?"* to
*"I get it, I want to try it"* in 60–90 seconds, then runs
`pip install novafabric`.

## What this is not

- Not a viewer. Cannot consume user capsules.
- Not a control plane. No mutations, no captures, no replays.
- Not a server. Pure static output.
- Not a telemetry surface. Zero analytics, zero error reporting,
  zero CDN-loaded scripts or fonts.

## Stack

Astro 5 + React 19 islands + Tailwind v4 + React Flow (lineage DAG)
+ Shiki (build-time syntax highlighting) + Ajv (in-browser schema
validation against the real `/schemas/` files) + `@noble/ed25519`
(in-browser signature verification for the evidence bundle demo).

All dependencies are Tier A licenses (Apache-2.0 / MIT / BSD / ISC /
OFL fonts) per [ADR 0024](../docs/decisions.md).
A CI gate (`scripts/check-licenses.mjs`) walks the full transitive
tree and fails the build on any non-Tier-A SPDX id.

## Develop

```bash
nvm use                 # Node 20 LTS
npm ci
npm run build:fixtures  # python scripts/build_showcase_fixture.py
npm run dev             # localhost:4321
```

## Build

```bash
npm run lint            # tsc + astro check
npm run check:licenses  # Tier-A gate
npm run build           # static output to dist/
npm run preview         # serve dist/ locally
```

## Test

```bash
npm run test:e2e        # Playwright e2e
npm run test:visual     # Playwright pixel diff
npm run lhci            # Lighthouse CI
```

## Deploy

```bash
make site        # from the repo root: builds web/dist/ and prints what to copy
```

`npm run build` produces a fully static `web/dist/` — no server, no DNS sleight
of hand. That directory is the deployable artifact.

**The last step is not automated, and this section used to claim it was.** It
said the site "deploys to GitHub Pages or Cloudflare Pages"; the live site at
novafabric.ai is served by nginx, there is no `CNAME`, and no Pages workflow
exists in this repository. Somebody copies `web/dist/` to the web root by hand.

That gap has a visible cost: `docs/` has been publishable as 48 pages since
2026-08-05 and `novafabric.ai/docs/` still returns 404, because building is not
deploying.

**Two rules for whoever does the copy:**

1. **Sync the whole `_astro/` directory, not individual files.** Vite
   content-hashes every chunk. Copying only the main bundle leaves stale chunk
   names, the browser 404s on a dynamic import, and the page renders completely
   blank with no visible error.
2. **Copy the whole `dist/`, including `docs/`.** The docs routes are generated
   from the repository's `docs/*.md` at build time, so they only exist in a fresh
   build.

Automating this — a deploy workflow, or a pull on the host — is worth doing and
is not blocked by anything in this repository.

## Boundary discipline

Every showcase page carries the persistent `Showcase — explanatory,
not a viewer` badge. Footer reminds visitors that the CLI is the
canonical interface. Each page's call-to-action sends visitors away
from the site (`pip install novafabric` → `nova capture python
your_agent.py`). The site reinforces the non-goals stance instead of
eroding it.

## Showcase vs `nova serve` (the live dashboard)

This `web/` directory ships **two** build targets from the same component
library:

| Target | Build command | Data source | Audience |
|---|---|---|---|
| Public showcase site | `npm run build` → `dist/` | Baked-in fixtures (`src/data/fixtures/`) | First-time visitors at `novafabric.ai` |
| Embedded dashboard SPA | `npm run build:dashboard` → `src/novafabric/serve/static/` | Live `/api/*` endpoints from a running `nova serve --experimental` | Existing users with their own capsules |

The two share `LineageGraph`, `CapsuleInspector`, `RegistryBrowser`, and
`DiffSection` via a `dataSource` prop fallback (props absent → fixtures;
props present → live data). The showcase site does not consume user
data and has no backend. The dashboard runs against the user's local
SQLite + capsule files and is gated behind `--experimental`.

The dashboard's **limitations vs the CLI** are documented at
[`docs/dashboard.md`](../docs/dashboard.md) — read that before treating
the dashboard as a complete substitute for `nova` commands.
