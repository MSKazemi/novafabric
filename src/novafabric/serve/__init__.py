"""
NovaFabric experimental local dashboard.

Per ADR-0027 — opt-in via `pip install novafabric[serve]` and gated behind
`nova serve --experimental`. Exposes a localhost-only HTTP API over the
existing registry SQLite, lineage SQLite, and capsule directories.

Layer A (v0.7) is read-only. Layers B and C (mutations, control plane) are
deferred to v0.8 and v0.9 pending review.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["create_app"]


def create_app(
    *,
    token: str,
    capsule_dir: Path,
    db_path: Path | None = None,
    static_dir: Path | None = None,
) -> object:
    """Lazy factory — defers FastAPI import until called.

    This keeps `nova --help` working even when the [serve] extra is not
    installed. The actual implementation lives in `app.py`.
    """
    from novafabric.serve.app import create_app as _create_app

    return _create_app(
        token=token,
        capsule_dir=capsule_dir,
        db_path=db_path,
        static_dir=static_dir,
    )
