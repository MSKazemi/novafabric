# Monitoring & Self-Observability

This guide covers monitoring NovaFabric's own HTTP apps — the `nova server`
REST API and the `nova serve` dashboard backend — with the self-observability
surface shipped in v0.61: Prometheus `/metrics`, split `/livez` / `/readyz`
probes, the structured `/v0/version` identity endpoint, and opt-in
self-tracing into the deployment's own OTLP ingest
([ADR-0182](../decisions.md); companion
spec: `design/spec/ops-observability-surface-v0.md`).

**Status: experimental** (v0.61, shipped 2026-07-16). Endpoint paths are
stable in intent, but metric names and label sets may still change before the
v1.0 freeze.

> **The two apps, in one line:** `nova server` is the team/API surface (OIDC,
> RBAC, `/v0`, default port 7433); `nova serve` is the localhost dashboard
> (single shared token, default port 4321). Both expose the same
> observability surface, gated by their own auth model.
>
> **This is not telemetry.** Nothing here phones home. Metrics are scraped by
> *your* Prometheus; self-tracing spans go to *your own* OTLP ingest and the
> server refuses non-loopback trace endpoints unless you explicitly override
> (see [Self-tracing](#self-tracing-opt-in-default-off)).

---

## 1. Endpoint summary

| Endpoint | App | Auth | Purpose |
|---|---|---|---|
| `/livez` | both | none | Process liveness only — never checks dependencies |
| `/readyz` | both | none | Itemized dependency readiness; 503 when any check fails |
| `/metrics` | both | server: `reader` role (exemptable); serve: dashboard token | Prometheus text exposition |
| `/v0/version` | server only | `reader` role | Structured build/deployment identity |
| `/health` (server), `/api/health` (serve) | both | none | Pre-existing compatibility aliases, unchanged |

`/metrics` requires `prometheus-client`, which ships in the `[server]` extra
(`pip install 'novafabric[server]'`). Without it the apps still run and
`/metrics` answers 503 with an install hint.

## 2. `/livez` vs `/readyz`

**`/livez`** answers `{"status": "ok"}` whenever the process can serve HTTP.
It deliberately checks nothing else — use it as a Kubernetes `livenessProbe`
so a slow database never gets your pod killed.

**`/readyz`** runs itemized dependency checks and names each one in the body:

```bash
curl -s http://127.0.0.1:7433/readyz
```

```json
{"status": "ok", "checks": {"db": "ok", "migrations": "ok", "object_store": "skipped"}}
```

Per-check status vocabulary:

| Status | Meaning | Fails readiness? |
|---|---|---|
| `ok` | Check passed | no |
| `fail` | Check failed (or raised) | **yes — 503, `"status": "degraded"`** |
| `skipped` | Dependency not configured (e.g. no object store) | no |
| `unknown` | Not determinable cheaply — honest degradation, never a fake `ok` | no |

The compiled-in checks today:

- **`db`** — a cheap `SELECT 1` against the configured backend (SQLite file
  or Postgres DSN, 2 s timeout).
- **`migrations`** — compares the SQLite `alembic_version` stamp against the
  in-repo migration head. Reports `unknown` when the DB is not
  alembic-managed or the migration scripts aren't available (installed
  package). On the Postgres backend this check is always `unknown` today —
  the head comparison needs a live connection plus the postgres migration
  tree and is deliberately not done in the probe path.
- **`object_store`** — `skipped` on both apps: neither has an object-store
  configuration surface wired into the probe yet; the spec mandates checking
  it only when configured.

The `/readyz` body carries **only check names and statuses** — never DSNs,
hostnames, or error strings (normative privacy rule). Failure details go to
the server log.

## 3. `/v0/version`

Role-gated (`reader`) structured identity of the running build:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:7433/v0/version
```

Fields (all values are detected, never fabricated — `"unknown"` is the honest
fallback):

| Field | Source |
|---|---|
| `version` | Installed `novafabric` package version |
| `git_sha` | `NOVA_BUILD_SHA` env var, else `git rev-parse HEAD` from a source checkout, else `"unknown"` |
| `schema_revision` | Alembic stamp of the metadata DB, else the registry's integer `schema_version`, else `"unknown"` (always `"unknown"` on the Postgres backend today) |
| `extras` | Optional extras whose declared dependencies are all installed (derived from package metadata) |
| `features` | Live feature flags: `self_tracing`, `metrics`, `oidc`, `scim` |

## 4. Metrics

### Inventory

All metrics carry the `nova_` prefix and an `app` label (`server` | `serve`).
The normative inventory lives in
`design/spec/ops-observability-surface-v0.md` §"Initial metric inventory";
what is registered today:

| Metric | Type | Labels | Notes |
|---|---|---|---|
| `nova_http_requests_total` | counter | `app`, `route`, `method`, `status` | every handled request |
| `nova_http_request_duration_seconds` | histogram | `app`, `route`, `method` | request latency |
| `nova_ingest_events_total` | counter | `app`, `encoding`, `outcome` | ingest write routes; `outcome` is `accepted` (2xx/3xx) or `rejected`. On the server app the classified routes are the capsule ZIP upload (`encoding=zip`) and the external score submission (`encoding=json`) |
| `nova_readyz_check_status` | gauge | `app`, `check` | 1 = passing (`ok`/`skipped`/`unknown`), 0 = failing; updated on each `/readyz` probe |
| `nova_db_pool_in_use`, `nova_db_pool_size` | gauge | `app`, `pool` | **Sampled since v0.98.0 — but only when a pool exists.** Set `NOVAFABRIC_METADATA_DB_POOL=1` (ADR-0221, experimental, opt-in, Postgres only) and the metadata store's pool is read **at `/metrics` scrape time** (pull-based, no background sampler) into `pool="metadata"`. Without the opt-in — and always on SQLite, which has no pool — the store exposes no stats and the gauges stay unsampled rather than reporting a fabricated zero |

The spec also names a `nova_queue_depth` gauge; it is **planned** and not
registered yet.

### Privacy and cardinality rules (normative)

- The `route` label is always the registered **route template**
  (`/v0/runs/{id}`), never the raw request path; unmatched paths collapse to
  the single value `"unmatched"`.
- No tenant, workspace, or user identifiers ever appear in metric labels.
- Each app instance uses a dedicated Prometheus registry (never the global
  default), so embedding or repeated app construction cannot trip
  duplicate-timeseries errors.

### Access control

- **`nova server`:** `/metrics` requires the `reader` role by default
  (ADR-0182 D1). To scrape without credentials, set the operator exemption —
  `observability.metrics_exempt: true` in `nova-server.yaml`, or
  `NOVAFABRIC_SERVER_METRICS_EXEMPT=1`.
- **`nova serve`:** `/metrics` sits behind the existing dashboard token.

### Scraping with Prometheus

With the exemption enabled (or a bearer credential configured), a minimal
scrape config:

```yaml
scrape_configs:
  - job_name: novafabric-server
    static_configs:
      - targets: ["nova-server.internal:7433"]
    # If /metrics stays role-gated, pass a token instead of exempting:
    # authorization:
    #   type: Bearer
    #   credentials_file: /etc/prometheus/nova-server.token
  - job_name: novafabric-serve
    static_configs:
      - targets: ["127.0.0.1:4321"]
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/nova-serve.token
```

**Grafana note.** Any Prometheus-backed Grafana works out of the box. Useful
starting panels: request rate by route
(`sum by (route) (rate(nova_http_requests_total[5m]))`), p95 latency
(`histogram_quantile(0.95, sum by (le, route) (rate(nova_http_request_duration_seconds_bucket[5m])))`),
429 pressure (`rate(nova_http_requests_total{status="429"}[5m])` — see the
[quotas & rate limits guide](quotas-and-rate-limits.md)), and the
`nova_readyz_check_status` gauges as a readiness stat row. NovaFabric does
not ship a packaged Grafana dashboard today.

## 5. Self-tracing (opt-in, default OFF)

**Status: experimental** (ADR-0182 D5). When enabled, the server app emits
**one OTLP/JSON span per HTTP request** into the deployment's *own* OTLP
trace ingest — by default the local serve app's `POST /api/otlp/v1/traces`
endpoint on `http://127.0.0.1:4321`. NovaFabric monitors itself with its own
flight recorder; spans never leave the deployment.

Enable via `nova-server.yaml`:

```yaml
observability:
  self_tracing: true
  # self_tracing_endpoint: http://127.0.0.1:4321/api/otlp/v1/traces  # the default
```

or environment: `NOVAFABRIC_SERVER_SELF_TRACING=1`
(+ `NOVAFABRIC_SERVER_SELF_TRACING_ENDPOINT` to override the target,
`NOVAFABRIC_SELF_TRACE_TOKEN` for the ingest's bearer token — a transport
credential only, never recorded in spans).

Guarantees, by construction:

- **No phone-home.** Non-loopback endpoint hosts are refused loudly at
  startup unless `NOVAFABRIC_SELF_TRACE_ALLOW_REMOTE=1` is set — and that
  override exists only for deployment-internal collectors.
- **Fail-open and non-blocking.** Spans go through a bounded in-process queue
  (default 1024) with a drop counter; a full queue or failed POST drops the
  batch and counts it — no retries, and request handling is never blocked or
  broken by tracing.
- **Attribute privacy.** Spans carry only the route template, method, status
  code, and timing. Never request bodies, auth headers, or tenant/workspace/
  user identifiers (normative).

Self-tracing is wired on the **server** app today; the serve app registers
`/livez`, `/readyz`, and `/metrics` but has no self-tracing switch yet.

## 6. Kubernetes probe example

```yaml
livenessProbe:
  httpGet: { path: /livez, port: 7433 }
  periodSeconds: 10
readinessProbe:
  httpGet: { path: /readyz, port: 7433 }
  periodSeconds: 10
```

The probe and metrics endpoints are **never rate-limited** when the ADR-0179
limiter is enabled — probes must not brown out with the API (see
[quotas & rate limits](quotas-and-rate-limits.md)).

---

## 7. Operational alerts (experimental, ADR-0192)

**Status: experimental** — first slice. NovaFabric can push an alert when a
condition an on-call human should hear about fires, instead of waiting for a
poll. The alert is an ordinary ADR-0137 lifecycle event from the new `ops.*`
family, carrying a first-class `severity` (`info | warning | critical`), and
rides the exact same emitter stack (hygiene scan, optional HMAC signing,
bounded-retry webhook delivery) — there is no second dispatcher.

Wired sources today: **`ops.quota.breached`** — emitted (severity `critical`)
when a capsule write is rejected by an ADR-0179 hard storage quota — plus the
other five `ops.*` types, all now wired: `ops.rate_limit.sustained`
(`server/rate_limit.py`), `ops.policy.violation` (`registry/service.py`),
`ops.drift.detected` (`cli/drift.py`), `ops.seal.verify_failed` (`cli/verify.py`),
and `ops.backup.failed` (`cli/backup.py`).

Default **OFF**: with no `NOVA_ALERTS_*` configuration nothing is emitted or
sent — byte-identical to previous releases. Minimal opt-in:

```bash
export NOVA_ALERTS_WEBHOOK="https://alertgw.internal.example/nova"  # allowlist; NO default
export NOVA_ALERTS_MIN_SEVERITY="warning"    # per-endpoint minimum (positional list allowed)
export NOVA_ALERTS_DEDUP_WINDOW_S="300"      # ≤ 1 delivery per (type, subject, window)
```

Alert-specific guarantees on top of the ADR-0137 fail-safe rules:

- **Dedup** — at most one delivery per (event type, subject, window), held in
  a bounded in-process map (oldest evicted), so a flapping quota cannot page
  400 times;
- **Severity routing** — each endpoint declares a minimum severity;
- **Never blocks** — delivery runs off the request path; an unreachable
  endpoint never fails or slows the operation that raised the alert;
- **Auditable** — every delivery attempt appends one entry (endpoint id,
  event id, outcome, attempt count) to the hash-chained audit log
  (`NOVA_ALERTS_AUDIT_LOG`, default the house audit log).

### Notification adapters (Slack / PagerDuty / email)

**Status: experimental** — the same alert can be *rendered* into a
provider-specific shape instead of the generic webhook JSON. Adapters are
**renderers, not integrations**: each takes the canonical event and produces
the target payload over the same delivery core — no OAuth, no account
management, no bundled MTA, and no hardcoded SaaS URL (even PagerDuty's Events
API endpoint is your config). Zero new dependencies.

Select the adapter per endpoint with `NOVA_ALERTS_ADAPTER`
(`webhook` | `slack` | `pagerduty` | `email`; default `webhook`, so unset
behavior is unchanged). One value applies to all endpoints; a comma-separated
list maps positionally to `NOVA_ALERTS_WEBHOOK`. An unknown value falls back to
`webhook` with a warning.

```bash
# Slack — incoming-webhook JSON (text + minimal blocks) to your webhook URL
export NOVA_ALERTS_WEBHOOK="https://hooks.slack.com/services/T000/B000/XXXX"
export NOVA_ALERTS_ADAPTER="slack"

# PagerDuty — Events API v2 (severity maps critical→critical, warning→warning,
# info→info); the endpoint URL and routing key are both your config
export NOVA_ALERTS_WEBHOOK="https://events.pagerduty.com/v2/enqueue"
export NOVA_ALERTS_ADAPTER="pagerduty"
export NOVA_ALERTS_PAGERDUTY_ROUTING_KEY="R0UT1NGK3Y"

# email — RFC 5322 via stdlib smtplib to your SMTP relay (no default relay)
export NOVA_ALERTS_WEBHOOK="smtp"          # a slot to position the endpoint; unused for email
export NOVA_ALERTS_ADAPTER="email"
export NOVA_ALERTS_SMTP_HOST="mail.internal.example"
export NOVA_ALERTS_SMTP_PORT="25"
export NOVA_ALERTS_EMAIL_FROM="novafabric@ops.example"
export NOVA_ALERTS_EMAIL_TO="oncall@ops.example"
```

### Dashboard read endpoint — `GET /api/alerts/recent`

**Status: experimental.** `nova serve` exposes recent alert activity for the
dashboard (shared-token auth, same as the other read routes):

```
GET /api/alerts/recent?limit=50        # limit 1..200, default 50
```

It returns the delivery audit trail (`alert.delivery` entries — endpoint,
outcome, attempts) **merged** with recent `ops.*` events from the durable
`events.jsonl` (emitted-but-maybe-undelivered ⇒ outcome `emitted`), newest
first and capped at `limit`. It is a bounded read (no capsule scans) and
fail-safe — a missing audit log or events file yields an empty list, never a
500:

```json
{
  "alerts": [
    {
      "id": "…", "timestamp": "2026-07-17T12:00:00+00:00",
      "event_type": "ops.quota.breached", "severity": "critical",
      "subject": "quota:capsules", "outcome": "delivered",
      "endpoint_id": "webhook-1", "attempts": 1
    }
  ],
  "total": 1,
  "alerting_configured": true
}
```

`alerting_configured` reflects whether a `NOVA_ALERTS_*` endpoint is configured.

Full variable reference and normative semantics:
`design/spec/lifecycle-webhooks-v0.md` §"Operational alerts";
decision record: `design/adr/0192-alerting-notification-bus.md`.

---

## See also

- [Quotas & rate limits](quotas-and-rate-limits.md) — the 429 surface these
  metrics make visible
- [Server admin guide](server-admin-guide.md) — auth, roles, and tokens used
  to gate `/metrics` and `/v0/version`
- [Server deployment](server-deployment.md) — deployment topologies
- [ADR-0182](../decisions.md) — the
  decision record; `design/spec/ops-observability-surface-v0.md` — the
  normative endpoint/metric contract
