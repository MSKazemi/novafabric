"""Alembic dual-track migration package for MetadataStore.

Two independent migration trees:
- sqlite/  — dev-only, single-process SQLite migrations
- postgres/ — production Postgres migrations (includes RLS, FORCE RLS, roles)

Use ``alembic upgrade head`` from within each sub-directory, or call
``bootstrap()`` on the store class for the v0.1 inline DDL path.
"""
