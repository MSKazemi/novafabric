"""Locate the capsule an ``@agent(capsule_dir=ROOT)`` call wrote (M13).

``capsule_dir`` is a capsule store: each decorated call writes
``ROOT/<run_id>/``. Tests that make exactly one call use :func:`only_capsule`.
"""
from __future__ import annotations

from pathlib import Path


def sdk_capsules(root: Path) -> list[Path]:
    """Every capsule directory directly under ``root``, sorted by run id."""
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "capsule.yaml").is_file())


def only_capsule(root: Path) -> Path:
    """The single capsule under ``root``; fails loudly if there is not exactly one."""
    found = [p for p in root.iterdir() if p.is_dir()]
    assert len(found) == 1, f"expected one capsule under {root}, found {found}"
    return found[0]
