# Server Administration Guide (multi-user `nova server`)

This guide is for the administrator of a shared, multi-user NovaFabric
deployment — the `nova server` REST API (`/v0`), not the single-operator
`nova serve` dashboard. It covers identity, roles, tenancy, and audit
administration. For installation and deployment topologies see the
[Server Deployment Guide](server-deployment.md); this guide does not repeat it.

Everything below is labelled per the docs honesty rule: **works today**,
**experimental**, **planned**, or **future design**.

> **The two servers, in one line:** `nova server` is the team/API surface
> (OIDC, RBAC, `/v0`); `nova serve` is the localhost dashboard (single shared
> token). See [`docs/api-reference.md`](../api-reference.md) for the full
> disambiguation table.

---

## 1. Enabling server mode

**Status: experimental** (v0.7+,
[ADR-0017](../decisions.md),
[ADR-0029](../decisions.md)).

```bash
pip install 'novafabric[server]'
nova server start                       # 127.0.0.1:7433 by default
nova server start --host 0.0.0.0 --port 8080
```

Configuration lives in `nova-server.yaml` (`$NOVA_SERVER_CONFIG`); secrets are
**environment-only** and stripped from YAML (`NOVA_DSN`, `NOVAFABRIC_SCIM_TOKEN`,
…). See [Server Deployment Guide — Scenario 2](server-deployment.md) for the
Postgres backend and migration from SQLite.

> **Local auth (ADR-0184, experimental — shipped 2026-07-16).** With OIDC
> disabled, the server requires an auto-generated local bearer token (printed
> at startup, stored at `~/.novafabric/.server-token` mode 0600, pinnable via
> `NOVAFABRIC_SERVER_TOKEN`). The old anonymous-admin behavior needs the
> explicit `--insecure-no-auth` opt-out and refuses non-loopback binds
> without `--i-know-this-is-public`
> ([ADR-0184](../decisions.md)).

## 2. Identity backends

| Backend | Status | Notes |
|---|---|---|
| OIDC (JWT Bearer + JWKS) | **experimental** | The primary team backend. Setup: [Server Deployment Guide — Scenario 3](server-deployment.md). JWKS cache flush: `nova server flush-jwks-cache`. [ADR-0018](../decisions.md) |
| Offline ed25519 tokens | **experimental** | Air-gapped/CI/SLURM machine identity without an IdP. `nova server issue-token --subject worker-01 --roles reader,writer --expires-in 30d`; revoke with `nova server revoke-token <jti>`. Revocations are recorded in the `token_audit` table. |
| SCIM 2.0 provisioning | **experimental** | Off by default (endpoints 404). Enable with `NOVAFABRIC_SERVER_SCIM_ENABLED=1` **and** `NOVAFABRIC_SCIM_TOKEN`. `/scim/v2/*` per RFC 7644; all provisioning actions land in the append-only `scim_audit_events` table. [ADR-0139](../decisions.md) |
| SAML 2.0 SSO | **experimental, license-gated** | SP metadata via `nova server saml-metadata`. The ACS endpoint deliberately returns **501** until an XML-DSIG verification library clears the dependency-license gate ([ADR-0138](../decisions.md) §D5) — NovaFabric never skips assertion signature verification. |
| Device-grant demo flow | **off by default (endpoints 404)** | The RFC 8628 `/v0/auth/device/code\|token\|approve` flow is local/testing scaffolding whose HS256 tokens the real verifier never honours; `/approve` is unauthenticated. It stays unmounted unless you set `NOVAFABRIC_SERVER_DEMO_DEVICE_GRANT=1`. **Never enable it in production** — use OIDC or offline tokens instead. [ADR-0198](../decisions.md) |

## 3. Roles and authorization

**Status: experimental.**

[ADR-0018](../decisions.md) defines six built-in roles.
Four are enforced as route-level checks:

| Role | Grants |
|---|---|
| `reader` | Read access to runs, capsules, lineage |
| `writer` | `reader` + create/ingest |
| `admin` | Everything, including role management |
| `auditor` | Orthogonal — audit trails and evidence only; cannot write |

The hierarchy is `reader < writer < admin`; `admin` satisfies every check;
`auditor` satisfies only auditor checks. The remaining two roles —
`promoter` and `approver` — implement separation of duties in the
maker-checker promotion and NovaSeal approval flows
([ADR-0058](../decisions.md)); the same
identity can never serve as both maker and checker for one proposal.

Assigning roles (either surface writes the same `role_assignments` table):

```bash
nova server assign-role --subject alice@example.com --role writer
# or over HTTP (admin-gated):
#   GET/POST /v0/admin/roles, DELETE /v0/admin/roles/{subject}/{role}
```

**Planned** (`future design`): org/workspace-scoped role bindings and named
service accounts — [ADR-0178](../decisions.md).

## 4. Tenancy and isolation

**Status: experimental** (Postgres backend only).

With the Postgres metadata store, tenant isolation is enforced **in the
database**, not in application code: `FORCE ROW LEVEL SECURITY` with a
`tenant_isolation` policy on `runs`, `capsules`, `signatures`, and
`retention_policies`, a per-transaction `SET LOCAL app.current_tenant_id`
(safe under pgBouncer transaction pooling), and a split between the
`novafabric_app` role (no `BYPASSRLS`) and `novafabric_migrator`
([ADR-0040](../decisions.md),
[ADR-0052](../decisions.md)).
A CI gate (`metadata_store_security_gate`) re-proves cross-tenant isolation on
every change.

Tenancy today is a single flat `tenant_id` per deployment-defined scope. There
is **no** organization/workspace/team hierarchy yet — that is
[ADR-0178](../decisions.md)
(`future design`), which keeps `tenant_id` as the sole RLS key.

## 5. Audit trails

**Status: works today** (local + server).

- Hash-chained append-only audit log; domain trails for SCIM
  (`scim_audit_events`), tokens (`token_audit`), SAML (redacted records), and
  retention decisions ("deletion is evidence", [ADR-0134](../decisions.md)).
- `nova audit …` maps capsule evidence to regulatory controls;
  `nova ledger anchor|verify|status` runs the adversary-anchored
  accountability ledger ([ADR-0094](../decisions.md));
  `nova seal log …` operates the Merkle log.
- Give compliance staff the `auditor` role — read-only by construction.

## 6. API behavior an admin should know

- **Versioning:** all resource routes sit under `/v0`; the canonical contract
  is [`api/openapi.yaml`](../../api/openapi.yaml). A formal deprecation/sunset
  policy is proposed in [ADR-0188](../decisions.md)
  (`future design`).
- **Pagination:** cursor-based, default 50 / max 500 per page (**works
  today**). `GET /v0/capsules` now serves **keyset** cursors — see 6a
  (**experimental**).
- **Rate limiting/quotas:** **experimental, default off** — in-process rate
  limiting + storage quotas ([ADR-0179](../decisions.md)),
  plus per-workspace usage metering, `GET /v0/usage` reporting, and
  per-workspace budgets
  ([ADR-0208](../decisions.md)).
  See [Quotas & rate limits](quotas-and-rate-limits.md); with the master
  switch off (the default), plan capacity accordingly.
- **Health:** `GET /health` (unauthenticated). There is **no** Prometheus
  `/metrics`, `/livez`/`/readyz` split, or version endpoint yet — proposed in
  [ADR-0182](../decisions.md)
  (`future design`). For diagnostics today use `nova doctor`
  (add `--check-storage` for schema/migration state).

### 6a. Bulk capsule operations + keyset pagination (experimental, ADR-0206)

**Status: experimental** — shipped by
[ADR-0206](../decisions.md). The normative contract is
specified in the maintainers' private `design/` tree and is not published.

**Keyset pagination on `GET /v0/capsules`:**

- List order is pinned to `created_at DESC, run_id DESC`; `next_cursor` is
  an opaque v1 keyset cursor — pass it back verbatim. Page requests are
  O(page) against a derived runs-cache index (lazily backfilled from the
  capsule directory, which stays the source of truth) instead of re-parsing
  every `capsule.yaml`.
- `total` appears on first-page (and legacy-cursor) responses only; cursor
  pages omit it by design.
- A corrupted/tampered cursor is now a **400 `invalid_cursor`** — it no
  longer silently restarts from the first page.
- Legacy `{"offset": N}` cursors still work for one deprecation cycle
  (ADR-0188); those responses carry `Deprecation: true`. Set
  `server.pagination.legacy_offset_cursors: false`
  (`NOVAFABRIC_SERVER_PAGINATION_LEGACY_OFFSET_CURSORS=false`) to refuse
  them early.

**Governed deletion (`admin` role only — writer/reader/auditor get 403):**

- `DELETE /v0/capsules/{run_id}` — removes the capsule directory plus its
  derived index rows (runs-cache, content-search) and appends a
  `capsule_delete` audit entry. Re-deleting is a 404.
- `POST /v0/capsules/bulk-delete` — body
  `{"run_ids": [...], "dry_run": false}`; per-item outcomes
  (`deleted | held | not_found | invalid_id | duplicate | error`) plus
  summary counts; no rollback — partial progress is reported, not undone.
  Batch size is capped by `server.bulk.max_items`
  (`NOVAFABRIC_SERVER_BULK_MAX_ITEMS`, default 100, ceiling 1000);
  oversized batches are a 422 before any work. `dry_run: true` returns the
  identical report and deletes nothing.
- **Legal holds always win:** any unreleased hold in any registry's
  `holds.jsonl` refuses deletion (409 `legal_hold_active` / per-item
  `held`) and there is **no force override**. Honest limit: holds are
  registry-global today — one active hold freezes the whole delete
  surface. Unexpired WORM locks refuse with 409 `worm_hold`.
  **Since v0.98.0 a corrupt line in `holds.jsonl` fails closed:** an
  unparseable line is treated as an *active, blocking* hold (and logged as a
  warning) rather than skipped, because a truncated or damaged line may encode
  the very hold that must block the delete. Symptom: deletion refuses with
  `legal_hold_active` and no hold is visible in `nova hold list` — repair or
  remove the damaged line before retrying.
- Audit actions: `capsule_delete` (per deletion, `via: api|bulk`),
  `capsule_delete_refused` (single-delete 409s), `capsule_bulk_delete`
  (one summary per bulk request, dry runs included).
- Bulk **export** is separate and already shipped —
  [ADR-0141](../decisions.md)
  (`nova export-batch`).

## 7. Backup and restore

See the dedicated [Backup & Restore Runbook](backup-restore.md) — manual
procedures that work today, the `nova backup` tooling
([ADR-0181](../decisions.md),
`experimental`), and the automated Postgres restore
(`nova restore` — pg profile auto-detected, ADR-0217,
[ADR-0211](../decisions.md),
`experimental`).

### 7a. Startup schema-skew guard (experimental, ADR-0211)

Before the server touches its database at startup, the lifespan compares the
DB's Alembic stamp against the installed build's migration head (registry
track; both `sqlite` and `postgres` backends):

- schema **behind** the build → the server **refuses to start**
  (`E-SKEW-BEHIND`), naming both revisions and the fix:
  `nova db upgrade --track registry --backend <backend>`;
- stamp **unknown to this build** (migrated by a newer/different NovaFabric)
  → refuses (`E-SKEW-AHEAD`): upgrade the package, do **not** downgrade the
  schema;
- **unstamped** DB (any `init_schema()`-bootstrapped deployment) or an
  **unknown** state → starts with one structured warning — never a refusal
  based on ignorance, never a fake `ok`.

Break-glass: `NOVAFABRIC_ALLOW_SCHEMA_SKEW=1` downgrades refusals to a
structured warning (`event=schema_skew_overridden`) and starts — for
emergency read-mostly access only; unset after the incident. Refusal happens
**before** `init_schema()` and the org bootstrap, so a refused server mutates
nothing. Error/warning text carries backend name and revisions only — never
DSNs or hostnames. `/readyz`'s `migrations` check uses the same comparator
and reports real `ok`/`fail` for Postgres too.

## 8. Enterprise hardening at a glance

First slices shipped `experimental` 2026-07-16; tracked in the
enterprise-readiness plan:

| Feature | ADR | Status |
|---|---|---|
| Secure-by-default local auth (no anonymous admin) | [0184](../decisions.md) | experimental |
| Workspaces, organizations, service accounts (`/v0/orgs`, `/v0/workspaces`, `/v0/service-accounts`) | [0178](../decisions.md) | experimental |
| Rate limiting + storage quotas (default off; quota enforcement is warn-then-reject) | [0179](../decisions.md) | experimental |
| `/metrics`, `/livez`, `/readyz`, `/v0/version` | [0182](../decisions.md) | experimental |
| Support bundle (`nova support-bundle`) | [0187](../decisions.md) | experimental |
| Backup sets (`nova backup create/verify`, `nova restore` local profile) | [0181](../decisions.md) | experimental |
| Automated pg restore (`nova restore`, manifest-driven — [0217](../decisions.md)) + startup schema-skew guard ([0211](../decisions.md) Part B) | 0217 / 0211 | experimental |
| Backup sets (`nova backup create/verify`, local profile; restore planned) | [0181](../decisions.md) | experimental |

## 9. Webhook subscriptions (experimental)

**Status: experimental** ([ADR-0205](../decisions.md),
spec `webhook-registry-v0.md`) — API
shapes may change. Server mode only; the env-configured `NOVA_EVENTS_*` /
`NOVA_ALERTS_*` sinks are unchanged.

API-managed outbound event subscriptions: `/v0/webhooks` CRUD + test ping, a
persisted per-attempt delivery log with explicit redelivery, and HMAC-signed
delivery through the ADR-0137 sink core.

```yaml
# ~/.config/novafabric/server.yaml
webhooks:
  enabled: true                  # default false — off ⇒ no dispatch worker at all
  queue_max: 1000                # bounded dispatch queue (overflow = drop-with-audit)
  max_attempts: 5                # POSTs per delivery chain (1–10); backoff 0s/30s/2m/10m/1h
  timeout_s: 5.0                 # per-POST timeout
  delivery_retention_days: 30    # delivery-log age cap
  delivery_retention_rows: 10000 # delivery-log per-webhook row cap
  allow_insecure_url: false      # permit non-loopback http:// endpoints (audited opt-out)
  allow_internal_targets: false  # v0.98.0 SSRF guard opt-out — see below
```

Env overrides follow `NOVAFABRIC_SERVER_WEBHOOKS_*` (plus the spec's
`NOVAFABRIC_WEBHOOKS_QUEUE_MAX` alias for the queue bound).

Operating notes:

- **RBAC:** mutations (create/update/delete/ping/redeliver) are `admin`-only;
  list/get and the delivery log are `admin` or `auditor`; `reader`/`writer`
  have no access.
- **SSRF guard (v0.98.0, works today).** A webhook URL whose host resolves to a
  private, link-local, reserved, or unspecified address is **rejected at
  create/update time** — the server refuses to be used as a probe of your
  internal network. **Loopback is always allowed**, so local webhooks keep
  working unchanged. A self-hosted deployment that legitimately posts to an
  internal receiver (`http://alertmanager.internal/...`) sets
  `webhooks.allow_internal_targets: true` — a documented, auditable opt-out,
  exactly like `allow_insecure_url`. The rejection message names the blocked
  address and the setting that overrides it. **Config-file only:** unlike the
  other webhook settings there is no `NOVAFABRIC_SERVER_WEBHOOKS_*` env
  override for this one today, so it must be set in `server.yaml`.
- **Signing secret** (`nvwh_…`) is returned **exactly once** by
  `POST /v0/webhooks` and is never retrievable afterwards. At rest it is
  wrapped via the ADR-0185 key-wrapping path when
  `NOVAFABRIC_WEBHOOKS_KEK_PATH` names a local 256-bit KEK file; otherwise it
  is stored **as-is** in the 0600 registry DB (a documented fallback —
  `secret_at_rest: plaintext|wrapped` on every GET shows your posture).
  Secret rotation is planned (P2); until then, delete + recreate.
- **Verify deliveries** with the `X-NovaFabric-Signature: t=<unix>,v1=<hex>`
  header: `v1 = HMAC-SHA256(secret, "{t}." + raw_body)`; reject when
  `|now − t| > 300 s`. A reference verifier ships as
  `novafabric.server.webhooks.verify_delivery_signature`.
- **Delivery contract:** best-effort notification, not evidence transport —
  5 bounded attempts, then the row is terminal `failed` and visible in
  `GET /v0/webhooks/{id}/deliveries`; recover with
  `POST /v0/webhooks/{id}/deliveries/{delivery_id}/redeliver`. A dead endpoint
  never delays or fails ingest (bounded queue, drop-with-audit on overflow).
  Scheduled retries are in-memory (P1): rows left `pending`/`retrying` across
  a restart are not auto-resumed — redeliver them manually.
- **Audit:** every lifecycle change, delivery attempt, and queue overflow
  appends to the hash-chained audit log (and flows to SIEM export as
  `webhook.*` OCSF rows). The secret never appears in any audit entry.
