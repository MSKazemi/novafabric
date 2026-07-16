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
[ADR-0017](../../design/adr/0017-server-api-protocol.md),
[ADR-0029](../../design/adr/0029-server-config-schema.md)).

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
> ([ADR-0184](../../design/adr/0184-secure-by-default-local-server-auth.md)).

## 2. Identity backends

| Backend | Status | Notes |
|---|---|---|
| OIDC (JWT Bearer + JWKS) | **experimental** | The primary team backend. Setup: [Server Deployment Guide — Scenario 3](server-deployment.md). JWKS cache flush: `nova server flush-jwks-cache`. [ADR-0018](../../design/adr/0018-auth-model.md) |
| Offline ed25519 tokens | **experimental** | Air-gapped/CI/SLURM machine identity without an IdP. `nova server issue-token --subject worker-01 --roles reader,writer --expires-in 30d`; revoke with `nova server revoke-token <jti>`. Revocations are recorded in the `token_audit` table. |
| SCIM 2.0 provisioning | **experimental** | Off by default (endpoints 404). Enable with `NOVAFABRIC_SERVER_SCIM_ENABLED=1` **and** `NOVAFABRIC_SCIM_TOKEN`. `/scim/v2/*` per RFC 7644; all provisioning actions land in the append-only `scim_audit_events` table. [ADR-0139](../../design/adr/0139-scim-provisioning.md) |
| SAML 2.0 SSO | **experimental, license-gated** | SP metadata via `nova server saml-metadata`. The ACS endpoint deliberately returns **501** until an XML-DSIG verification library clears the dependency-license gate ([ADR-0138](../../design/adr/0138-saml-sso-server-mode.md) §D5) — NovaFabric never skips assertion signature verification. |

## 3. Roles and authorization

**Status: experimental.**

[ADR-0018](../../design/adr/0018-auth-model.md) defines six built-in roles.
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
([ADR-0058](../../design/adr/0058-maker-checker-dual-approval.md)); the same
identity can never serve as both maker and checker for one proposal.

Assigning roles (either surface writes the same `role_assignments` table):

```bash
nova server assign-role --subject alice@example.com --role writer
# or over HTTP (admin-gated):
#   GET/POST /v0/admin/roles, DELETE /v0/admin/roles/{subject}/{role}
```

**Planned** (`future design`): org/workspace-scoped role bindings and named
service accounts — [ADR-0178](../../design/adr/0178-workspace-organization-model.md).

## 4. Tenancy and isolation

**Status: experimental** (Postgres backend only).

With the Postgres metadata store, tenant isolation is enforced **in the
database**, not in application code: `FORCE ROW LEVEL SECURITY` with a
`tenant_isolation` policy on `runs`, `capsules`, `signatures`, and
`retention_policies`, a per-transaction `SET LOCAL app.current_tenant_id`
(safe under pgBouncer transaction pooling), and a split between the
`novafabric_app` role (no `BYPASSRLS`) and `novafabric_migrator`
([ADR-0040](../../design/adr/0040-production-metadata-store-interface.md),
[ADR-0052](../../design/adr/0052-pgbouncer-transaction-mode-role-split.md)).
A CI gate (`metadata_store_security_gate`) re-proves cross-tenant isolation on
every change.

Tenancy today is a single flat `tenant_id` per deployment-defined scope. There
is **no** organization/workspace/team hierarchy yet — that is
[ADR-0178](../../design/adr/0178-workspace-organization-model.md)
(`future design`), which keeps `tenant_id` as the sole RLS key.

## 5. Audit trails

**Status: works today** (local + server).

- Hash-chained append-only audit log; domain trails for SCIM
  (`scim_audit_events`), tokens (`token_audit`), SAML (redacted records), and
  retention decisions ("deletion is evidence", [ADR-0134](../../design/adr/0134-data-retention-policy-scheduler.md)).
- `nova audit …` maps capsule evidence to regulatory controls;
  `nova ledger anchor|verify|status` runs the adversary-anchored
  accountability ledger ([ADR-0094](../../design/adr/0094-adversary-anchored-ledger-and-replay-attestation.md));
  `nova seal log …` operates the Merkle log.
- Give compliance staff the `auditor` role — read-only by construction.

## 6. API behavior an admin should know

- **Versioning:** all resource routes sit under `/v0`; the canonical contract
  is [`api/openapi.yaml`](../../api/openapi.yaml). A formal deprecation/sunset
  policy is proposed in [ADR-0188](../../design/adr/0188-api-deprecation-sunset-policy.md)
  (`future design`).
- **Pagination:** cursor-based, default 50 / max 500 per page (**works today**).
- **Rate limiting/quotas:** **none today** — plan capacity accordingly; the
  design is [ADR-0179](../../design/adr/0179-api-rate-limiting-quotas.md)
  (`future design`).
- **Health:** `GET /health` (unauthenticated). There is **no** Prometheus
  `/metrics`, `/livez`/`/readyz` split, or version endpoint yet — proposed in
  [ADR-0182](../../design/adr/0182-self-observability-surface.md)
  (`future design`). For diagnostics today use `nova doctor`
  (add `--check-storage` for schema/migration state).

## 7. Backup and restore

See the dedicated [Backup & Restore Runbook](backup-restore.md) — manual
procedures that work today, plus the proposed `nova backup` tooling
([ADR-0181](../../design/adr/0181-backup-restore-dr-tooling.md), `future design`).

## 8. Enterprise hardening at a glance

First slices shipped `experimental` 2026-07-16; tracked in the
[enterprise-readiness plan](../../design/enterprise-readiness-plan-2026-07.md):

| Feature | ADR | Status |
|---|---|---|
| Secure-by-default local auth (no anonymous admin) | [0184](../../design/adr/0184-secure-by-default-local-server-auth.md) | experimental |
| Workspaces, organizations, service accounts (`/v0/orgs`, `/v0/workspaces`, `/v0/service-accounts`) | [0178](../../design/adr/0178-workspace-organization-model.md) | experimental |
| Rate limiting (default off; quotas parse, enforcement planned) | [0179](../../design/adr/0179-api-rate-limiting-quotas.md) | experimental |
| `/metrics`, `/livez`, `/readyz`, `/v0/version` | [0182](../../design/adr/0182-self-observability-surface.md) | experimental |
| Support bundle (`nova support-bundle`) | [0187](../../design/adr/0187-support-bundle-diagnostics.md) | experimental |
| Backup sets (`nova backup create/verify`, local profile; restore planned) | [0181](../../design/adr/0181-backup-restore-dr-tooling.md) | experimental |
