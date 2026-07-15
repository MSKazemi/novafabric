# `novafabric.serve`

Experimental **local, read-only dashboard** (ADR-0027). Opt-in via
`pip install novafabric[serve]`, gated behind `nova serve --experimental`.
Exposes a localhost-only HTTP API over the existing registry SQLite, lineage
SQLite, and capsule directories. Layer A (v0.7) is read-only.

**Not to be confused with [`novafabric.server`](../server/) — the multi-user
production REST API.** `serve` = single-user local viewer; `server` = hosted API.
