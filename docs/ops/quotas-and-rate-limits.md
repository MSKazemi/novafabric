# API Rate Limiting & Storage Quotas

This guide covers protecting a shared `nova server` deployment from noisy
clients: in-process API rate limiting and warn-then-reject storage quotas
([ADR-0179](../decisions.md); normative
budgets in `design/spec/rate-limiting-quotas-v0.md` (private)).

**Status: experimental** (v0.61, shipped 2026-07-16) — and **disabled by
default**. With the feature off (the default), no middleware is installed at
all: no headers, no timing side effects, zero behavior change on upgrade.

Both tracks apply to the `nova server` app only; the `nova serve` dashboard
is not rate-limited.

---

## 1. Rate limiting

### Design in one paragraph

A stdlib-only **token bucket** per key and limit class, on a monotonic clock,
held in a bounded in-process map (LRU-evicted above 10,000 buckets). It is
deliberately **not distributed** — the supported topology is a single-writer
server, so an in-process limiter is the honest fit (ADR-0179).

### Limit classes and defaults

Every request is classified, in order: exemptions → admin prefixes → OTLP
ingest paths → write verbs → reads.

| Class | Matches | Default rate (tokens/s) | Default burst |
|---|---|---|---|
| `ingest` | write verbs (POST/PUT/…) + any OTLP ingest path, regardless of verb | 100 | 200 |
| `read` | GET / HEAD | 50 | 100 |
| `admin` | `/v0/admin/*`, `/v0/roles/*` | 10 | 20 |

**Always exempt — never limited, never counted, no headers:** `/health`,
`/livez`, `/readyz`, `/metrics`. Probes must not brown out with the API, and
`/metrics` is how you *see* the limiting.

### Bucket keying

The bucket key is resolved per request in strict fallback order — first
available wins:

1. **principal** — the SHA-256 digest of the presented bearer token (the raw
   token is never used as a key value);
2. **tenant** — a resolved tenant id (the slot exists for the ADR-0178
   workspace model; no server surface populates it before routing today);
3. **client IP** — unauthenticated surfaces.

Classes never share buckets, so an ingest storm cannot starve reads for the
same client.

### The 429 contract

A limited request receives `429` with the standard ADR-0017 error envelope
(`error.code = "rate_limited"`) plus:

- `Retry-After` — whole seconds until one token is available (ceiling);
- `X-RateLimit-Limit` — the class burst (bucket capacity);
- `X-RateLimit-Remaining` — whole tokens still available;
- `X-RateLimit-Reset` — delta-seconds until the bucket is full again.

Successful responses on classed routes carry the three `X-RateLimit-*`
headers too, so well-behaved clients can pace themselves before hitting 429.

### Enabling and tuning

In `nova-server.yaml`:

```yaml
rate_limits:
  enabled: true
  ingest: { rate: 100, burst: 200 }
  read:   { rate: 50,  burst: 100 }
  admin:  { rate: 10,  burst: 20 }
  audit_threshold_rejections: 100   # rejections per key per window that trigger an audit event
  audit_window_seconds: 60
```

or via environment overrides: `NOVAFABRIC_SERVER_RATE_LIMITS_ENABLED=1`,
`NOVAFABRIC_SERVER_RATE_LIMITS_{INGEST,READ,ADMIN}_{RATE,BURST}`,
`NOVAFABRIC_SERVER_RATE_LIMITS_AUDIT_THRESHOLD_REJECTIONS`,
`NOVAFABRIC_SERVER_RATE_LIMITS_AUDIT_WINDOW_SECONDS`.

Watch the effect in Prometheus:
`rate(nova_http_requests_total{status="429"}[5m])` — 429 rejections are
counted by the metrics middleware (see [monitoring](monitoring.md)).

### Audit trail

Sustained limiting of one key — at least `audit_threshold_rejections`
rejections within one `audit_window_seconds` window — emits **one audit
record per key per window** (`rate_limit_sustained`), carrying the key
*digest* (`sha256:…`), the limit class, the rejection count, and the window
start. The raw key value (token, IP) is never stored. A structured warning is
always logged for operator visibility.

**Honest note:** these records go to the same append-only JSONL audit log the
server's role-management routes already use. That log is append-only but
**not hash-chained**; the spec's hash-chaining requirement lands when the
hash-chained `AuditLog` is wired into the server app (see the ADR-0179
status note).

## 2. Storage quotas

### Semantics

Quotas bound what the capsule store holds — capsule **count** and total
stored **bytes** — with **warn-then-reject** enforcement, checked at capsule
ingest only (the capsule write routes):

- **Soft limit reached** (`usage >= soft`) — the write **succeeds**, and the
  response carries a warning header:
  `X-NovaFabric-Quota-Warning: capsules 950/1000` (comma-joined when both
  kinds warn). One audit event per kind per audit window
  (`quota_soft_exceeded`).
- **Hard limit reached** (`usage >= hard`) — the write is **rejected** with
  `429`, `error.code = "quota_exceeded"`, and `details` carrying
  `kind` / `usage` / `limit`. There is deliberately **no `Retry-After`
  header** — a quota does not decay on a clock; the remedy is deleting or
  archiving capsules, or raising the limit. Audit event:
  `quota_hard_exceeded`.

Usage is **derived from the existing capsule store** (directories holding a
`capsule.yaml`, plus every file's size) — no new ledger to reconcile — and
cached with a short monotonic TTL (5 s) so hot ingest paths don't re-count
the store per request. Quota audit events carry no key hash: this global
track is **per-deployment**; per-workspace budgets are a separate additive
track (ADR-0208, §3 below, **experimental**) that reads metered counters
instead of the store walk.

### Enabling

Quotas require the same master switch as rate limiting
(`rate_limits.enabled: true`) plus a `quota` block with at least one non-zero
limit. `0` means unlimited; all-zero limits keep the feature fully inert.

```yaml
rate_limits:
  enabled: true
  quota:
    max_capsules_soft: 900
    max_capsules_hard: 1000
    max_bytes_soft:  8_000_000_000    # 8 GB
    max_bytes_hard: 10_000_000_000    # 10 GB
```

Hard limits must be ≥ their soft counterpart (validated at config load).

## 3. Per-workspace budgets & usage metering

**Status: experimental** ([ADR-0208](../decisions.md);
normative contract in `design/spec/usage-metering-v0.md` (private)). Behind the same
master switch (`rate_limits.enabled`); with the switch off there is no
accounting, no middleware, and no new tables — zero behavior change.

### Usage metering

Every accepted capsule upload is attributed to a workspace at ingest and
recorded in an append-only `usage_ledger` + transactional `usage_counters`
in the registry DB (metrics: `capsules_created`, `bytes_stored` = unpacked
size, `api_requests` via a bounded accumulator flushed at most once per
`usage.flush_interval_s`). Attribution order: API-key workspace binding →
the principal's single workspace membership → the `default` workspace —
recorded on each row (`attribution = key|membership|default`), so
unattributed usage is visible, not laundered. Counting is idempotent by
construction: a retried upload hits the 409-duplicate path, and a replayed
ledger insert is a no-op under the `(metric, ref)` unique index. Deleting a
capsule (single or bulk, ADR-0206) appends **negative** ledger rows so
budgets reflect reclaimed storage; capsules uploaded before metering shipped
appear only in the derived `global`/`drift` figures.

Monthly rollups freeze each finished period lazily at the first write of the
next one (no cron) and are retention-bounded
(`usage.rollup_retention_months`, default 24; raw ledger rows
`usage.ledger_retention_months`, default 3).

**Reporting — `GET /v0/usage`** (`?period=YYYY-MM`, default current):
per-workspace metric totals, org rollups, and — for admin/auditor only — the
`global` store-derived figures plus a `drift` block (derived minus metered).
Other principals get a **filtered** response (their membership workspaces
only) — filtering, not 403. Accounting is best-effort relative to the
upload: a metering failure is logged + audited and never fails the write.

### Per-workspace budgets

An additive `workspaces` map inside the existing quota block; the global
limits keep their exact meaning and both checks run (strictest wins).
Enforcement reads the **metered** counters, not the store walk:

```yaml
rate_limits:
  enabled: true
  quota:
    workspaces:              # ADR-0208; absent => byte-identical behavior
      ml-platform:
        max_capsules_soft: 1000
        max_capsules_hard: 2000
        max_bytes_soft: 0    # 0 = unlimited, per existing convention
        max_bytes_hard: 0
```

Same ladder and contract: soft ⇒ write succeeds +
`X-NovaFabric-Quota-Warning: <workspace>/<kind> <usage>/<limit>` (comma-
joined after any global warning) + one audit event per (workspace, kind)
per window; hard ⇒ `429 quota_exceeded` with an additive `workspace` field
in `details`, no `Retry-After`. A config that names a workspace slug that
does not exist is refused at startup. Alerting (ADR-0192): a hard rejection
fires `ops.quota.breached` (critical, subject `quota:<workspace>:<kind>` —
per-workspace dedup); the first soft crossing per window fires the same
event at `warning` severity (subject suffixed `:soft`). Both are no-ops
unless `NOVA_ALERTS_*` is configured.

Env overrides: `NOVAFABRIC_SERVER_USAGE_{METERING_ENABLED,FLUSH_INTERVAL_S,`
`ACCUMULATOR_MAX_ENTRIES,ROLLUP_RETENTION_MONTHS,LEDGER_RETENTION_MONTHS}`.
`usage.metering_enabled: false` keeps rate limits on while switching
accounting off.

**Honest limits:** attribution consumes the ADR-0193 API-key workspace
binding, which is *stored but not enforced* at request time — metering
inherits that gap; this feature meters and scopes, it does **not** isolate
the capsule store (ADR-0178 gap unchanged); `api_requests` counts are
deliberately coarse (bounded accumulator, crash loses at most one interval).

## 4. What this feature is not

- **Not distributed.** Buckets and quota caches are in-process. If you run
  multiple server replicas (not the supported single-writer topology),
  limits apply per replica.
- **Not applied to `nova serve`.** The dashboard app has no limiter.
- **Not tenant isolation.** Per-workspace budgets and metering (§3) are
  scoping and accounting on top of the shared capsule store — the ADR-0178
  isolation gap is unchanged and Security-Architect gated.

---

## See also

- [Monitoring & self-observability](monitoring.md) — how to see 429s and
  request rates
- [Server admin guide](server-admin-guide.md) — roles, tokens, and audit
  trails
- [ADR-0179](../decisions.md) — the
  decision record; `design/spec/rate-limiting-quotas-v0.md` (private) — normative
  defaults and the 429/quota contracts
