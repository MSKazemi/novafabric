"""Which capsule ``nova mcp-proxy`` / ``nova api-proxy`` record into.

``NOVAFABRIC_CAPSULE_DIR`` means two things (review cli-b-15). Inside a capture child —
the process ``nova capture`` and every runner start, which also set
``NOVAFABRIC_SPAN_ID`` — it is the **active capsule**. In a user's shell it is the
documented capsule **store root** (``nova capture``, ``nova query``, ``nova retention``
read it so). Treating a store root as the active capsule wrote a capsule called
``capsules`` straight into the store, its streams mixed with every real capsule
directory.

Resolution, in order:

1. ``--capsule-dir`` — that capsule (created if missing);
2. ``NOVAFABRIC_CAPSULE_DIR`` with ``NOVAFABRIC_SPAN_ID`` — the active capsule of the
   capture this proxy runs inside;
3. ``NOVAFABRIC_CAPSULE_DIR`` alone — a store root: a fresh ULID capsule under it;
4. neither — a fresh ULID capsule under ``$PWD/.novafabric/runs/``.
"""
from __future__ import annotations

import os
from pathlib import Path

from novafabric.capture._ulid import new_ulid


def resolve_proxy_capsule(explicit: Path | None) -> tuple[Path, bool]:
    """``(capsule_dir, freshly_allocated)`` for a proxy, by the rules above."""
    if explicit is not None:
        return explicit, False
    env_dir = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
    if env_dir:
        if os.environ.get("NOVAFABRIC_SPAN_ID"):
            return Path(env_dir), False
        return Path(env_dir) / new_ulid(), True
    return Path.cwd() / ".novafabric" / "runs" / new_ulid(), True
