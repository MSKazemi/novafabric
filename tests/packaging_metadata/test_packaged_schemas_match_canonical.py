"""The wheel's schemas must not silently fall behind the canonical ones.

Two trees hold the same JSON Schemas:

``schemas/``
    canonical, and what every design doc and ADR cites.
``src/novafabric/schemas/``
    the copy that goes into the wheel — and the *only* one the installed CLI
    can see at runtime (``cli/validate.py`` resolves ``SCHEMA_DIR`` relative to
    its own ``__file__``).

They are **not** required to be identical, and this test deliberately does not
assert that. The two trees encode two schema generations: canonical carries the
frozen OAS v1.0 target (``schema_version`` must match ``^1\\.``, ADR-0034 §1),
while the packaged copy matches what the code actually writes today
(``0.1.0``). Asserting equality would break `nova validate` for every ordinary
capsule — verified by running it.

What *is* required: the packaged copy must never be **missing a property** the
canonical one declares. That is the failure mode observed on 2026-08-01. The
ADR-0196 commits (``b66d5803``, ``b6a98ddf``, ``015dca06``) added the ``facets``
container to the canonical ``run-capsule`` schema and never touched the
packaged one. Because the schema is ``additionalProperties: false``, the
installed CLI then rejected the project's own headline extension point::

    ✗ capsule.yaml: Additional properties are not allowed ('facets' was unexpected)

An omission is always a bug; a differing *definition* may be a deliberate
generational difference, so those are listed explicitly below rather than
asserted away.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANONICAL_DIR = _REPO_ROOT / "schemas"
_PACKAGED_DIR = _REPO_ROOT / "src" / "novafabric" / "schemas"

#: Properties whose *definition* legitimately differs between the two
#: generations. Each entry is a deliberate decision, not drift.
_GENERATIONAL_DIFFERENCES: dict[str, set[str]] = {
    # Canonical pins the frozen v1.0 spec (`^1\.`); the packaged copy accepts
    # the 0.1.0 the producers actually write. Reconciling the two is a
    # public-format migration and needs its own ADR.
    "*": {"schema_version"},
    # Canonical carries the Phase 3 causal/compositional vocabulary
    # (contains/spawned/delegated_to/…, ADR-0044 + ADR-0122 D3). The packaged
    # copy carries the data-flow vocabulary (consumed/produced_by/…) that
    # `lineage/_writer.py` actually emits. Disjoint on purpose.
    "lineage-edge.schema.json": {"edge_type"},
}

_DUPLICATED = sorted(
    p.name
    for p in _CANONICAL_DIR.glob("*.json")
    if (_PACKAGED_DIR / p.name).exists()
)


def test_there_are_duplicated_schemas_to_check() -> None:
    """Guard the guard: a bad glob would make every case below vacuous."""
    assert len(_DUPLICATED) >= 16, _DUPLICATED


@pytest.mark.parametrize("name", _DUPLICATED)
def test_packaged_schema_declares_every_canonical_property(name: str) -> None:
    """An omitted property is always drift, never a decision."""
    canonical = json.loads((_CANONICAL_DIR / name).read_text())
    packaged = json.loads((_PACKAGED_DIR / name).read_text())

    missing = sorted(
        set(canonical.get("properties", {})) - set(packaged.get("properties", {}))
    )
    assert not missing, (
        f"src/novafabric/schemas/{name} is missing {missing}, which "
        f"schemas/{name} declares. The packaged copy is what the installed CLI "
        "validates against, and these schemas are additionalProperties:false — "
        "so a capsule using those fields is rejected by `nova validate` even "
        "though it is valid. Port the property across (do NOT copy the whole "
        "file; see this module's docstring)."
    )


@pytest.mark.parametrize("name", _DUPLICATED)
def test_no_undocumented_definition_drift(name: str) -> None:
    """Definitions may differ only where this module says they may."""
    canonical = json.loads((_CANONICAL_DIR / name).read_text())
    packaged = json.loads((_PACKAGED_DIR / name).read_text())

    allowed = _GENERATIONAL_DIFFERENCES["*"] | _GENERATIONAL_DIFFERENCES.get(name, set())
    canonical_props = canonical.get("properties", {})
    packaged_props = packaged.get("properties", {})

    unexplained = sorted(
        key
        for key in set(canonical_props) & set(packaged_props)
        if key not in allowed and canonical_props[key] != packaged_props[key]
    )
    assert not unexplained, (
        f"{name}: {unexplained} are defined differently in schemas/ and "
        "src/novafabric/schemas/, and that difference is not recorded in "
        "_GENERATIONAL_DIFFERENCES. Either port the canonical definition "
        "across, or add it there with the reason."
    )
