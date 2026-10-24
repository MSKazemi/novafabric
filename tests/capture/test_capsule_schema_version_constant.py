"""Every capsule writer stamps one ``schema_version`` (review eval-adapters-query-18).

``nova capture``, ``@agent``, OTLP ingest, the shared adapter core and five adapters
wrote ``0.1.0`` while ``a2a``, ``google_adk`` and ``openai_agents`` wrote ``1.0.0``
(``bedrock_agentcore`` too, before it moved onto the core) — one format, two labels.
Under ADR-0034 the format is a pre-freeze draft at ``0.1.0``; ``1.0.0`` is what
``nova migrate`` produces, with a ``migration_source`` lineage edge. A capsule
labelled ``1.0.0`` by a writer has neither, and ``nova migrate`` refuses it as
"not 0.1.0".

Static, like ``tests/adapters/test_adapter_manifests_match_the_schema.py``: every
manifest literal in the source tree must take its version from the constant.
"""
from __future__ import annotations

import ast
from pathlib import Path

from novafabric._capsule_version import CAPSULE_SCHEMA_VERSION
from novafabric.capsule_migration._converter import SOURCE_VERSION

_SRC = Path(__file__).resolve().parents[2] / "src" / "novafabric"
#: A dict literal is a capsule manifest when it carries all three keys.
_MARKERS = {"schema_version", "run_id", "capture_mode"}


def _manifest_versions() -> dict[str, ast.expr]:
    found: dict[str, ast.expr] = {}
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Dict):
                continue
            keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
            if _MARKERS <= {k for k in keys if isinstance(k, str)}:
                found[f"{path.relative_to(_SRC)}:{node.lineno}"] = node.values[
                    keys.index("schema_version")
                ]
    return found


def test_the_writers_are_found() -> None:
    """Guard the guard: orchestrator, @agent, OTLP ingest, the core and 4 own-body adapters."""
    assert len(_manifest_versions()) >= 8, sorted(_manifest_versions())


def test_every_manifest_literal_uses_the_constant() -> None:
    offenders = {
        where: ast.unparse(value)
        for where, value in _manifest_versions().items()
        if not (isinstance(value, ast.Name) and value.id == "CAPSULE_SCHEMA_VERSION")
    }
    assert offenders == {}


def test_the_constant_is_what_nova_migrate_accepts() -> None:
    assert CAPSULE_SCHEMA_VERSION == SOURCE_VERSION == "0.1.0"
