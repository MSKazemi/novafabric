"""Test helpers for the batch capsule blob export suite (ADR-0141)."""

from __future__ import annotations

from pathlib import Path

import yaml


def make_capsule(
    root: Path,
    run_id: str,
    *,
    created_at: str = "2026-07-01T00:00:00Z",
    content: str = "hello",
    with_run_id: bool = True,
) -> Path:
    """Create a minimal on-disk capsule directory."""
    capsule = root / run_id
    (capsule / "outputs").mkdir(parents=True)
    meta: dict[str, str] = {"created_at": created_at}
    if with_run_id:
        meta["run_id"] = run_id
    (capsule / "capsule.yaml").write_text(yaml.dump(meta))
    (capsule / "outputs" / "stdout.txt").write_text(content)
    return capsule
