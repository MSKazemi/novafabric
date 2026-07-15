# `novafabric.server`

Multi-user **production REST API** (v0.7; ADR-0017 / ADR-0029). Provides the
hosted server surface: OIDC auth, RBAC, offline tokens, pagination, and error
handling over the metadata store.

**Not to be confused with [`novafabric.serve`](../serve/) — the experimental
local read-only dashboard.** `server` = hosted multi-user API; `serve` = local viewer.
