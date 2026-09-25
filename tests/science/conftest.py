"""Shared fixtures for the ADR-0164 P2 science tests: an on-disk science capsule."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.science import (
    ScienceNode,
    attach_facet,
    attach_receipt,
    build_facet,
    build_receipt,
    digest_node,
)

SEALED_ROOT = digest_node("sealed capsule root")
RUN_ID = "01HXSCIENCE0000000000000001"


def science_nodes() -> list[ScienceNode]:
    """A hypothesis→claim chain plus a converging second observation."""
    h = digest_node("H1")
    d = digest_node("D1")
    r = digest_node("R1")
    o1 = digest_node("O1")
    o2 = digest_node("O2")
    s = digest_node("S1")
    c = digest_node("C1")
    return [
        ScienceNode(kind="hypothesis", node_id="H1", node_digest=h),
        ScienceNode(kind="experiment_design", node_id="D1", node_digest=d, parent=h),
        ScienceNode(kind="experiment_run", node_id="R1", node_digest=r, parent=d),
        ScienceNode(kind="observation", node_id="O1", node_digest=o1, parent=r),
        ScienceNode(kind="observation", node_id="O2", node_digest=o2, parent=r),
        ScienceNode(kind="result", node_id="S1", node_digest=s, parent=[o2, o1]),
        ScienceNode(kind="claim", node_id="C1", node_digest=c, parent=s),
    ]


def make_capsule(
    root: Path,
    *,
    receipt_kwargs: dict[str, Any] | None = None,
    with_facet: bool = True,
    bound_root: str | None = SEALED_ROOT,
    extra: dict[str, Any] | None = None,
) -> Path:
    """Write a minimal capsule directory with optional science facet + receipt."""
    capsule_dir = root / RUN_ID
    capsule_dir.mkdir(parents=True)
    manifest: dict[str, Any] = {
        "run_id": RUN_ID,
        "created_at": "2026-07-15T10:00:00Z",
        "finished_at": "2026-07-15T10:05:00Z",
        "status": "success",
    }
    if with_facet:
        manifest = attach_facet(manifest, build_facet(science_nodes(), bound_root=bound_root))
    if receipt_kwargs is not None:
        manifest = attach_receipt(
            manifest,
            build_receipt(capsule_root=bound_root if with_facet else None, **receipt_kwargs),
        )
    if extra:
        manifest.update(extra)
    (capsule_dir / "capsule.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    (capsule_dir / "lineage.jsonl").write_text('{"event": "x"}\n')
    seal = capsule_dir / ".seal"
    seal.mkdir()
    (seal / "root.json").write_text('{"root": "x"}\n')
    return capsule_dir


FULL_RECEIPT: dict[str, Any] = {
    "environment_digest": digest_node("uv.lock v1"),
    "seeds": [1337, 42],
    "data_digest": digest_node("dataset v1"),
    "code_digest": digest_node("analysis.py v1"),
    "determinism_class": "statistical",
}


@pytest.fixture
def capsule_factory(tmp_path: Path) -> Callable[..., Path]:
    """Build a science capsule under a fresh tmp dir."""
    counter = {"n": 0}

    def _make(**kw: Any) -> Path:
        counter["n"] += 1
        return make_capsule(tmp_path / f"c{counter['n']}", **kw)

    return _make
