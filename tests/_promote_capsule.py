"""A capsule for maker-checker tests to propose, bypass and verify (review cli-b-01).

``nova seal propose`` / ``bypass`` / ``verify`` now sign and recompute the SHA-256 of
the capsule's ``capsule.yaml``, so the capsule must exist. ``store=None`` writes into
the CLI's default store (``$NOVAFABRIC_HOME/capsules`` under the hermetic test env).
"""

from __future__ import annotations

from pathlib import Path


def make_capsule(capsule_id: str, store: Path | None = None) -> Path:
    if store is None:
        from novafabric._paths import default_capsule_dir

        store = default_capsule_dir()
    capsule = store / capsule_id
    capsule.mkdir(parents=True, exist_ok=True)
    (capsule / "capsule.yaml").write_text(f"run_id: {capsule_id}\nstatus: success\n")
    return capsule
