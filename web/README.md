# NovaFabric embedded dashboard (`web/`)

> This directory is the source of the **local dashboard** that
> `nova serve --experimental` serves. It is not the public website:
> `https://novafabric.ai` is built from its own repository, and this tree adds
> no public-site code (ADR-0299 in the [decisions index](../docs/decisions.md)).
>
> The canonical automation interface is the CLI (`pip install novafabric`).
> The dashboard is a satellite: every view surfaces the equivalent `nova`
> command. Its limitations versus the CLI are documented in
> [`docs/dashboard.md`](../docs/dashboard.md).

## What ships in the wheel

```bash
npm run build:dashboard   # astro build + scripts/copy-dashboard.mjs
# or, from the repo root:
make bundle
```

`scripts/copy-dashboard.mjs` writes **only the dashboard** into
`src/novafabric/serve/static/`, which is tracked by git and packaged into the
wheel:

| Entry | What it is |
|---|---|
| `dashboard/` | the client-only app shell (`src/pages/dashboard.astro`) |
| `_astro/` | the JS chunks, CSS, fonts and images the shell reaches — computed as a transitive closure from the shell, not copied wholesale |
| `favicon.svg` | referenced by the shell |
| `index.html` | from `serve-shell/`: forwards `/` to `/dashboard/`, keeping `?token=` |
| `404.html` | from `serve-shell/`: a product-local not-found page |

Sibling directories owned by other build steps (`topology/`) are left untouched.
No marketing page, `robots.txt`, sitemap or `/docs/**` page is packaged; the
script deletes any it finds from an older build. Guarded by
`tests/packaging_metadata/test_serve_static_is_dashboard_only.py`.

Rebuild and commit the bundle after any change under `src/` that the dashboard
reaches — otherwise `nova serve` users run a stale UI even when the source is
correct.

## Links out of the dashboard

Explanatory links (for example a command builder's "Full reference" link) go to
`https://novafabric.ai/...` as plain external links, built with
`publicSiteUrl()` from `src/lib/links.ts`. Nothing in local mode requires them:
offline, the dashboard and its API work and those links simply do not load.

## Layout

- `src/components/dashboard/**` — the dashboard. It imports only `src/lib/*` and
  `src/components/ui/*`.
- `src/components/dashboard/commands/generatedCommands.ts` — generated from the
  live Typer app; regenerate with `uv run python web/scripts/gen-command-registry.py`.
  `tests/serve/test_command_registry_coverage.py` fails if it drifts from the CLI.
- `serve-shell/` — the product-local `index.html` and `404.html`.
- **Legacy, not packaged:** the Astro marketing pages under `src/pages/`
  (`index`, `concepts`, `install`, `why`, `spec`, `showcase/*`, `docs/*`) and the
  components only they use. `https://novafabric.ai` serves its own versions of
  every one of those routes. Removing them from this tree, and renaming `web/`
  to a dashboard-only path, is **planned** (ADR-0299 stages C–E).

## Stack

Astro 7 + React 19 islands + Tailwind v4 + React Flow (lineage DAG).

All dependencies are Tier A licenses (Apache-2.0 / MIT / BSD / ISC /
OFL fonts) per [ADR 0024](../docs/decisions.md).
A CI gate (`scripts/check-licenses.mjs`) walks the full transitive
tree and fails the build on any non-Tier-A SPDX id.

The dashboard loads no CDN scripts or fonts and sends no analytics or error
reports.

## Develop

```bash
nvm use                 # Node >=22.12 (matches package.json engines and CI)
npm ci
npm run dev             # localhost:4321 — open /dashboard/
```

Point the dev dashboard at a running `nova serve --experimental` by pasting the
URL and token it prints into the connect screen.

## Test

```bash
npm run lint            # tsc --noEmit
npm run check:licenses  # Tier-A gate
npm run test:unit       # vitest
npm run test:live       # Playwright against a real `nova serve` (skips if no browser;
                        # PW_CHANNEL=chrome uses installed Chrome; NOVA_E2E_PYTHON picks the interpreter)
npm run test:e2e        # Playwright e2e
```
