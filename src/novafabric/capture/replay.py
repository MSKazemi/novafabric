from __future__ import annotations

from typing import Any

MINIMAL_REPLAY_POLICY: dict[str, Any] = {
    "schema_version": "0.1.0",
}


def minimal_replay_policy() -> dict[str, Any]:
    return dict(MINIMAL_REPLAY_POLICY)
