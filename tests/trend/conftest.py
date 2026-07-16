"""Shared fixtures for the offline trend-report tests (ADR-0131).

Reuses the ADR-0129 query-suite capsule factories — a trend report runs on
the same extraction path, so the fixtures must stay a single definition.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import pytest

# Re-exported pytest fixtures (registration happens via the import binding).
from query.conftest import (  # noqa: F401
    capsule_dir,
    make_capsule,
    model_call,
    score,
)

CapsuleFactory = Callable[..., Path]
RecordFactory = Callable[..., dict[str, Any]]


@pytest.fixture
def views_dir(tmp_path: Path) -> Path:
    """An isolated saved-views directory (never the project's)."""
    path = tmp_path / "views-store"
    path.mkdir()
    return path
