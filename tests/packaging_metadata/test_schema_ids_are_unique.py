"""Guard: one `$id` must resolve to one document (ADR-0223).

The repo keeps two schema trees — canonical `schemas/` (the OAS v1.0 *target*)
and packaged `src/novafabric/schemas/` (what an installed CLI actually
validates against). Eight pairs declared the **same `$id`** while disagreeing
about `schema_version`: the canonical file required `^1\\.` and the packaged one
accepted `^[0-9]+\\.[0-9]+\\.[0-9]+`. Measured on a real `nova capture` output,
the canonical Run Capsule schema *rejected* every capsule NovaFabric produces
while a file with the same identity accepted it.

Under JSON Schema an `$id` is an identity, so any registry, `$ref` resolver, or
bundler that caches by `$id` gets an arbitrary one of the two documents. That is
a defect independent of which schema is "right".

Two files sharing an `$id` is only acceptable when they are the *same document*
— a build-time duplicate of one canonical file. That distinction is what these
tests encode.

This is the third instance of the underlying class (BL-028 packaged-vs-canonical
drift, BL-037 runtime schema paths missing from the wheel, ADR-0223): **two
files, one identity, and only one of them is the one that runs.**
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CANONICAL = REPO_ROOT / "schemas"
PACKAGED = REPO_ROOT / "src" / "novafabric" / "schemas"


def _schemas_with_ids() -> dict[str, list[Path]]:
    """Every JSON document in either tree that declares an `$id`."""
    found: dict[str, list[Path]] = defaultdict(list)
    for root in (CANONICAL, PACKAGED):
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(doc, dict) and isinstance(doc.get("$id"), str):
                found[doc["$id"]].append(path)
    return found


def _load(path: Path) -> dict[str, object]:
    result: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return result


def test_no_two_differing_schemas_share_an_id() -> None:
    """The invariant. Identical copies may share an `$id`; different documents
    may not, because then the identity does not identify anything."""
    offenders: list[str] = []
    for schema_id, paths in sorted(_schemas_with_ids().items()):
        if len(paths) < 2:
            continue
        docs = [_load(p) for p in paths]
        if all(d == docs[0] for d in docs[1:]):
            continue  # same document in two places — allowed
        rel = ", ".join(str(p.relative_to(REPO_ROOT)) for p in paths)
        offenders.append(f"  {schema_id}\n    -> {rel}")
    assert not offenders, (
        "these $ids resolve to two DIFFERENT documents, so anything caching by "
        "$id gets an arbitrary one:\n" + "\n".join(offenders)
    )


def test_no_two_schemas_disagree_about_their_version_contract() -> None:
    """The sharpest form of the defect: same identity, incompatible
    `schema_version` pattern. That is what made the canonical Run Capsule
    schema reject every capsule `nova capture` writes."""
    offenders: list[str] = []
    for schema_id, paths in sorted(_schemas_with_ids().items()):
        if len(paths) < 2:
            continue
        patterns = {
            str((_load(p).get("properties", {}) or {}).get("schema_version", {}).get("pattern"))
            for p in paths
        }
        if len(patterns) > 1:
            rel = ", ".join(str(p.relative_to(REPO_ROOT)) for p in paths)
            offenders.append(f"  {schema_id}: patterns {sorted(patterns)}\n    -> {rel}")
    assert not offenders, (
        "same $id, incompatible schema_version contract:\n" + "\n".join(offenders)
    )


def test_target_schemas_say_they_are_not_in_force() -> None:
    """A reader opening either file must learn which one binds, without having
    to reconstruct ADR-0223 from the commit history."""
    missing: list[str] = []
    for path in sorted(CANONICAL.glob("*.schema.json")):
        sibling = PACKAGED / path.name
        if not sibling.exists():
            continue
        doc = _load(path)
        # A *target* schema is one deliberately given a distinct identity from
        # its in-force sibling. Matching on a "-v1" filename instead would also
        # catch schemas that are simply named v1 and identical in both trees
        # (e.g. ledger-checkpoint-v1), which are not target schemas at all.
        if doc.get("$id") == _load(sibling).get("$id"):
            continue
        comment = str(doc.get("$comment", ""))
        if "NOT yet in force" not in comment or "ADR-0223" not in comment:
            missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, (
        "target schemas must state their pre-freeze status in $comment "
        "(see ADR-0223):\n" + "\n".join(f"  {m}" for m in missing)
    )


def test_the_split_is_real_and_not_vacuous() -> None:
    """Keeps this file honest: if the two trees are ever collapsed into one
    (ADR-0223 OQ-2), these guards would pass trivially and should be removed
    rather than left looking like they still protect something."""
    ids = _schemas_with_ids()
    shared = {k: v for k, v in ids.items() if len(v) > 1}
    assert shared, (
        "no $id is shared between the two schema trees any more — either the "
        "trees were collapsed (good; delete this module and ADR-0223's OQ-2) or "
        "the discovery logic broke"
    )
