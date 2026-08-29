"""Every adapter-written ``capsule.yaml`` must satisfy the run-capsule schema.

Why this guard exists
---------------------
Found 2026-08-01: **all eight** framework adapters wrote a top-level ``tags``
key, and two of them also wrote a private ``*_ref`` key.  None of those three
names exists in ``schemas/run-capsule.schema.json``, which is
``additionalProperties: false`` — so ``nova validate`` *rejected the capsules
NovaFabric's own adapters produce*::

    ✗ capsule.yaml: Additional properties are not allowed
      ('a2a_tasks_ref', 'tags' were unexpected)

The schema already had the right homes for both: ``metadata`` (free-form
string-valued labels — exactly what ``tags`` held) and ``extensions``
(reverse-DNS-keyed vendor data).

The check is deliberately **static**.  A dynamic test would need each adapter's
third-party SDK installed to produce a real capsule, so seven of the eight would
skip on most machines — and skipping is how this bug survived to begin with.
Parsing the manifest literal out of the source needs nothing but the AST, so all
eight are always checked, everywhere.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ADAPTER_DIR = _REPO_ROOT / "src" / "novafabric" / "adapters"
_SCHEMA_PATH = _REPO_ROOT / "schemas" / "run-capsule.schema.json"

#: A dict literal is treated as a capsule manifest when it carries both of
#: these keys. Every adapter builds its manifest as one literal.
_MANIFEST_MARKERS = {"schema_version", "run_id"}


def _schema_properties() -> set[str]:
    schema = json.loads(_SCHEMA_PATH.read_text())
    assert schema.get("additionalProperties") is False, (
        "this guard assumes a closed schema; if additionalProperties was "
        "deliberately opened, revisit whether this test still says anything"
    )
    return set(schema["properties"])


def _manifest_literals(source: Path) -> list[tuple[int, list[str]]]:
    """(lineno, keys) for every capsule-manifest dict literal in *source*."""
    found = []
    for node in ast.walk(ast.parse(source.read_text())):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        ]
        if _MANIFEST_MARKERS.issubset(keys):
            found.append((node.lineno, keys))
    return found


_ADAPTER_FILES = sorted(
    p for p in _ADAPTER_DIR.glob("*.py") if p.name != "__init__.py"
)


def test_the_adapter_directory_is_not_empty() -> None:
    """Guard the guard: an empty glob would make every case below vacuous."""
    assert len(_ADAPTER_FILES) >= 8, _ADAPTER_FILES


def test_a2a_adapter_capsule_passes_the_real_validator(tmp_path: Path) -> None:
    """The one adapter whose SDK is a declared extra, checked end to end.

    The static guards above cover all eight; this one runs the *real*
    ``validate_capsule_json`` over a *real* adapter-written ``capsule.yaml``, so
    the static approximation can never drift away from what `nova validate`
    actually does. It is the exact reproduction of the original report::

        ✗ capsule.yaml: Additional properties are not allowed
          ('a2a_tasks_ref', 'tags' were unexpected)
    """
    import asyncio
    from unittest.mock import MagicMock, patch

    import yaml

    a2a_interceptors = pytest.importorskip(
        "a2a.client.interceptors", reason="a2a-sdk is the [a2a] extra"
    )
    import jsonschema

    from novafabric.adapters.a2a import NovaA2AInterceptor

    # Validate against the *packaged* schema — the copy the installed CLI
    # loads (cli/validate.py resolves SCHEMA_DIR relative to its own __file__).
    # tests/packaging_metadata/test_packaged_schemas_match_canonical.py keeps
    # that copy equal to the canonical schemas/ tree.
    packaged_schema = json.loads(
        (
            _REPO_ROOT / "src" / "novafabric" / "schemas" / "run-capsule.schema.json"
        ).read_text()
    )

    interceptor = NovaA2AInterceptor(tmp_path)
    card = MagicMock()
    card.name = "agent-a"

    async def _one_call() -> None:
        await interceptor.before(
            a2a_interceptors.BeforeArgs(
                input={"q": "hi"}, method="send_message", agent_card=card
            )
        )
        await interceptor.after(
            a2a_interceptors.AfterArgs(
                result={"a": "yo"}, method="send_message", agent_card=card
            )
        )

    with (
        patch("novafabric.capture.hooks.install_all"),
        patch("novafabric.capture.hooks.uninstall_all"),
    ):
        asyncio.run(_one_call())

    manifests = list(tmp_path.glob("*/capsule.yaml"))
    assert len(manifests) == 1, manifests
    manifest = yaml.safe_load(manifests[0].read_text())

    jsonschema.validate(manifest, packaged_schema)  # raises on any violation

    # And it must stay valid once a first-party ADR-0196 facet is attached —
    # the case the stale packaged schema used to reject outright.
    jsonschema.validate(
        {**manifest, "facets": {"conversation": {"turns": 1}}}, packaged_schema
    )

    # The information the invalid keys used to carry must survive the move.
    assert manifest["metadata"]["framework"] == "a2a"
    # ADR-0224: the capsule must say whether its wire stream is complete.
    # Asserted on REAL adapter output, not just grepped for in the source.
    assert manifest["metadata"]["wire_capture"] in {
        "installed", "installed-contended", "scoped-concurrent", "skipped-concurrent"
    }, manifest["metadata"]
    assert manifest["metadata"]["agent"] == "agent-a"
    assert (
        manifest["extensions"]["io.a2aproject"]["tasks_ref"] == "a2a-tasks.jsonl"
    )
    assert (manifests[0].parent / "a2a-tasks.jsonl").exists()


def _constant_values(node: ast.Dict) -> dict[str, str]:
    """The subset of a manifest literal whose values are plain string constants."""
    out = {}
    for k, v in zip(node.keys, node.values):
        if (
            isinstance(k, ast.Constant)
            and isinstance(k.value, str)
            and isinstance(v, ast.Constant)
            and isinstance(v.value, str)
        ):
            out[k.value] = v.value
    return out


def _schema_enums() -> dict[str, list[str]]:
    schema = json.loads(_SCHEMA_PATH.read_text())
    return {
        name: spec["enum"]
        for name, spec in schema["properties"].items()
        if isinstance(spec, dict) and "enum" in spec
    }


@pytest.mark.parametrize("adapter", _ADAPTER_FILES, ids=lambda p: p.stem)
def test_adapter_manifest_respects_schema_enums(adapter: Path) -> None:
    """The second half of the same bug.

    Fixing the unknown *keys* exposed that all eight adapters also wrote an
    unknown *value*: ``capture_mode: "adapter-<framework>"``, where the schema
    admits only ``cli-wrapper``/``sdk-decorator``/``otel-import``/``manual``.
    The adapters are in-process SDK instrumentation, so ``sdk-decorator`` is the
    honest existing category; the framework identity it used to carry is
    preserved in ``metadata.framework``.
    """
    enums = _schema_enums()
    for node in ast.walk(ast.parse(adapter.read_text())):
        if not isinstance(node, ast.Dict):
            continue
        values = _constant_values(node)
        if not _MANIFEST_MARKERS.issubset(values | dict.fromkeys(
            k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        )):
            continue
        for field, allowed_values in enums.items():
            actual = values.get(field)
            if actual is not None:
                assert actual in allowed_values, (
                    f"{adapter.name}:{node.lineno} writes {field}={actual!r}, "
                    f"which run-capsule.schema.json rejects (allowed: "
                    f"{allowed_values}). `nova validate` fails on every capsule "
                    "this adapter produces."
                )


@pytest.mark.parametrize("adapter", _ADAPTER_FILES, ids=lambda p: p.stem)
def test_adapter_manifest_uses_only_schema_properties(adapter: Path) -> None:
    allowed = _schema_properties()
    for lineno, keys in _manifest_literals(adapter):
        unknown = sorted(k for k in keys if k not in allowed)
        assert not unknown, (
            f"{adapter.name}:{lineno} writes capsule.yaml keys that "
            f"run-capsule.schema.json rejects: {unknown}. `nova validate` fails "
            "on every capsule this adapter produces. Use `metadata` for "
            "string labels and `extensions` (reverse-DNS keys) for vendor refs."
        )


# --------------------------------------------------------------------------
# Wire-capture honesty (ADR-0224 OQ-1 / BL-038)
# --------------------------------------------------------------------------


def test_every_adapter_records_whether_wire_capture_was_active() -> None:
    """A short model-calls stream has three causes; the capsule must say which.

    `capture.hooks` is process-global and single-owner (ADR-0224), so a capture
    can end up with a complete wire stream, no wire stream at all, or a stream
    that also contains a concurrent run's events. Leaving a reader to infer
    which from the stream's length is exactly the ambiguity an evidence system
    must not ship — "not captured" and "did not happen" look identical.

    Every adapter that installs hooks must therefore stamp
    `metadata.wire_capture` from `hooks.wire_capture_state()`. A ninth adapter
    added later fails here rather than shipping a silently ambiguous capsule.
    """
    import re

    adapters_dir = Path(__file__).resolve().parents[2] / "src" / "novafabric" / "adapters"
    offenders: list[str] = []
    for path in sorted(adapters_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "install_all" not in source:
            continue  # adapter does not drive the process-global hooks
        if "wire_capture_state" not in source or not re.search(r'["\']wire_capture["\']', source):
            offenders.append(path.name)
    assert not offenders, (
        "these adapters install the global capture hooks but never record "
        "metadata.wire_capture, so their capsules cannot say whether the wire "
        f"stream is complete: {offenders}"
    )


def test_wire_capture_state_covers_all_four_outcomes() -> None:
    """The marker is only useful if it distinguishes every case.

    ADR-0224 phase 2 added the fourth: a capture that owns no hooks but bound
    its own recorder and writer, so its own events reached its own capsule
    through the owner's single patch layer. Before phase 2 that capture recorded
    nothing and said ``skipped-concurrent``; conflating the two would tell a
    reader a complete stream was absent.
    """
    from novafabric.capture import hooks

    assert hooks.wire_capture_state("") == "skipped-concurrent"
    assert hooks.wire_capture_state(hooks._PARTICIPANT_PREFIX + "abc") == (
        "scoped-concurrent"
    )

    hooks._contended_owners.clear()
    token = hooks._OWNER_PREFIX + "uncontended"
    hooks._hook_owner = token
    try:
        assert hooks.wire_capture_state(token) == "installed"
        hooks._contended_owners.add(token)
        assert hooks.wire_capture_state(token) == "installed-contended"
    finally:
        hooks._hook_owner = None
        hooks._contended_owners.clear()

    # All four are distinct — a marker that collapses two states is not a marker.
    assert len({
        hooks.wire_capture_state(""),
        hooks.wire_capture_state(hooks._PARTICIPANT_PREFIX + "x"),
        hooks.wire_capture_state(hooks._OWNER_PREFIX + "y"),
    }) == 3
