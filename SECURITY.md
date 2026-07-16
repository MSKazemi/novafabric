# Security Policy

## Reporting a vulnerability

Please do **not** open a public GitHub issue for security vulnerabilities.

Use GitHub's [private vulnerability reporting](https://github.com/novafabric/novafabric/security/advisories/new) to report issues confidentially.

We will acknowledge reports within 5 business days and aim to release a fix within 30 days for confirmed vulnerabilities.

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
