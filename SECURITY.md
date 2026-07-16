# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Use GitHub's [private vulnerability reporting](https://github.com/novafabric/novafabric/security/advisories/new) to report issues confidentially.

We will acknowledge reports within 5 business days and aim to release a fix within 30 days for confirmed vulnerabilities.

## Vulnerability response

The severity-tiered targets below refine the general promise above. NovaFabric
is a pre-1.0 open-source project maintained by a small team: these are
**best-effort commitments, not a contractual SLA**. They cover both privately
reported vulnerabilities (disclosure flow above — unchanged) and dependency
CVEs surfaced by our automated scanners.

Severity follows CVSS as assigned by the advisory source; triage may adjust it
for NovaFabric's actual exposure (e.g. a vulnerable code path we never call).

| Severity | Triage (acknowledge + assess) | Fix or mitigate |
|----------|-------------------------------|-----------------|
| Critical | 72 hours                      | 14 days         |
| High     | 7 days                        | 30 days         |
| Moderate | —                             | 90 days         |
| Low      | best effort                   | best effort     |

"Fix or mitigate" includes a documented workaround or a default-off toggle
when a complete fix needs longer than the window.

### Dependency scanning and waivers

- CI runs [pip-audit](https://pypi.org/project/pip-audit/) against the locked
  dependency set on every pull request and weekly; HIGH/CRITICAL findings
  block merges.
- Release container images are scanned with trivy; CRITICAL findings with an
  available fix block the release.
- Exceptions go through the checked-in waiver file
  [`.pip-audit-waivers.toml`](.pip-audit-waivers.toml): every waiver carries
  the vulnerability id, a written justification, and an **expiry date**. An
  expired waiver fails CI by construction, so accepted risk is always
  time-boxed and re-reviewed — waivers cannot rot into permanence.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Scope

NovaFabric is local-first: in its default configuration it is a self-contained
CLI tool with no network surface. The always-present attack surfaces are:

- YAML parsing (malicious spec files)
- SQLite database access (local filesystem)
- CLI argument handling

Several **opt-in** surfaces exist and are disabled by default; each has a
STRIDE analysis in [`THREAT_MODEL.md`](THREAT_MODEL.md):

- **Server mode** (`nova server` / `nova serve`) — network-exposed REST API
  (OIDC/RBAC or token auth).
- **Outbound lifecycle webhooks** (`NOVA_EVENTS_WEBHOOK`) — the only outbound
  network path; no default destination, payloads are IDs/digests only and
  secret-scanned before emission, HMAC signing is optional and recommended.
- **SCIM 2.0 provisioning** (`/scim/v2`) — inbound, active only when both the
  config flag and the dedicated `NOVAFABRIC_SCIM_TOKEN` are set; otherwise 404.
- **SAML endpoints** (`/v0/auth/saml/*`) — SP metadata only today; assertion
  consumption is refused (501) until a license-cleared XML-signature verifier
  ships — signature validation is never skipped.
- **OTLP GenAI ingest** (`POST /api/otlp/v1/traces`) — token-authenticated;
  foreign span data is secret-scanned at write time.

If you find a way to make any disabled-by-default surface reachable without
explicit opt-in, that is a vulnerability — please report it.
