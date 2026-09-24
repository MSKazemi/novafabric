# `novafabric.serve`

Experimental **local dashboard** (ADR-0027). Opt-in via
`pip install novafabric[serve]`, gated behind `nova serve --experimental`.
Exposes a localhost-only HTTP API over the existing registry SQLite, lineage
SQLite, and capsule directories. Layer A (v0.7) is read-only; **Layer B
mutations ship as well** (register, promote, redact, export), each confirm-gated
and audit-logged, so "read-only dashboard" stopped describing this module at
v0.8 and is no longer written here.

**Authorization lives in [`authz.py`](authz.py)** (ADR-0228): four scopes —
`read` / `operate` / `admin` cumulative, plus an orthogonal `audit` — enforced
for every route by one declarative table and one app-level dependency. A route
missing from `ROUTE_SCOPES` is **denied to everyone**, including the server
token; `tests/serve/test_authz_route_table.py` fails on the first unclassified
route. Adding an endpoint therefore means adding a line there — see
`docs/developer-guide.md` § *Adding a new `nova serve` API endpoint*.

**Tenancy lives in [`tenancy.py`](tenancy.py)** (ADR-0229): the eight stores in the read path
each declare `aware` / `agnostic` / `unsafe`, checked against their real schema by
`tests/serve/test_tenancy_registry.py`. Three are `unsafe`, so **multi-tenant mode is refused at
startup** rather than served — `NOVAFABRIC_SERVE_TENANCY=multi` makes `nova serve` exit before
binding a socket, naming each blocking store. Adding a store to the read path means adding a
declaration there.

**Not to be confused with [`novafabric.server`](../server/) — the multi-user
production REST API.** `serve` = single-user local viewer; `server` = hosted API.
