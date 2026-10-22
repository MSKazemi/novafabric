"""L33: asset specs reject keys they do not define (``extra="forbid"``).

``spec/models.py`` used pydantic's default (ignore unknown keys), so a typo such as
``suite_name:`` for ``suite:`` validated and was dropped: the asset registered without
the field its author believed was set. Every spec model now forbids unknown keys; the
one explicit escape hatch for vendor data is the top-level ``extensions:`` mapping.

Behaviour change: a spec that carried an unknown key used to register and now fails
validation, naming the key. Every spec shipped in ``examples/``, ``tests/fixtures/``,
the docs, and the suggestion-engine templates still validates (pinned below).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from novafabric.spec.validator import SpecValidationError, validate_spec

ROOT = Path(__file__).resolve().parents[1]

_EVAL = """\
novafabric_spec_version: "1"
asset_type: evaluation
name: triage-suite
version: 1.0.0
spec:
  suite: triage_accuracy_suite
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "spec.yaml"
    p.write_text(text)
    return p


def test_valid_evaluation_spec(tmp_path: Path) -> None:
    spec = validate_spec(_write(tmp_path, _EVAL))
    assert spec.spec.suite == "triage_accuracy_suite"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("text", "key"),
    [
        (_EVAL.replace("  suite:", "  suite_name:"), "suite_name"),
        (_EVAL + "descripton: typo at the top level\n", "descripton"),
        (_EVAL.replace("asset_type: evaluation", "asset_type: evaluation\nownr: me"), "ownr"),
        (
            """\
novafabric_spec_version: "1"
asset_type: agent
name: a
version: 1.0.0
spec:
  model: {provider: openai, name: gpt-4o, temprature: 0.1}
  tools: []
  prompts: {}
  policies: []
  evals: [suite]
""",
            "temprature",
        ),
        (
            """\
novafabric_spec_version: "1"
asset_type: model
name: m
version: 1.0.0
spec: {framework: pytorch, artifact_path: m.pt, artefact_path: typo}
""",
            "artefact_path",
        ),
    ],
)
def test_unknown_key_is_a_validation_error_naming_it(
    tmp_path: Path, text: str, key: str
) -> None:
    with pytest.raises(SpecValidationError) as info:
        validate_spec(_write(tmp_path, text))
    assert key in str(info.value)
    assert info.value.hint and "extensions" in info.value.hint


def test_extensions_block_is_kept_verbatim(tmp_path: Path) -> None:
    text = _EVAL + "extensions:\n  com.example.owner: team-a\n  com.example.ticket: {id: 7}\n"
    spec = validate_spec(_write(tmp_path, text))
    assert spec.extensions == {"com.example.owner": "team-a", "com.example.ticket": {"id": 7}}


def _shipped_spec_files() -> list[Path]:
    files = sorted((ROOT / "examples").rglob("*.yaml")) + sorted(
        (ROOT / "tests" / "fixtures").glob("*.yaml")
    )
    return [p for p in files if "novafabric_spec_version" in p.read_text(errors="ignore")]


@pytest.mark.parametrize("path", _shipped_spec_files(), ids=lambda p: p.name)
def test_every_shipped_spec_still_validates(path: Path) -> None:
    if path.name == "missing_asset_type.yaml":  # the deliberate invalid fixture
        with pytest.raises(SpecValidationError, match="asset_type"):
            validate_spec(path)
        return
    validate_spec(path)


def _doc_spec_blocks() -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    for doc in ("docs/cli-reference.md", "docs/user-guide.md"):
        text = (ROOT / doc).read_text()
        for index, match in enumerate(re.finditer(r"```ya?ml\n(.*?)```", text, re.DOTALL)):
            body = match.group(1)
            if "novafabric_spec_version" in body:
                blocks.append((f"{doc}#{index}", body))
    return blocks


@pytest.mark.parametrize(("where", "body"), _doc_spec_blocks(), ids=lambda v: str(v)[:40])
def test_every_documented_spec_still_validates(tmp_path: Path, where: str, body: str) -> None:
    data = yaml.safe_load(body)
    assert isinstance(data, dict), where
    validate_spec(_write(tmp_path, body))


def test_suggestion_engine_drafts_still_validate(tmp_path: Path) -> None:
    from novafabric.registry import suggestion_engine as se

    drafts = [
        se._model_draft_yaml("gpt-4o", "1.0.0", "openai", "gpt-4o"),
        se._tool_draft_yaml("search", "mcp", "local"),
        se._agent_draft_yaml("agent-x", "0.1.0", ["python", "a.py"], "abc", "gpt-4o", "openai", ["search"]),
        se._agent_draft_yaml("agent-y", "0.1.0", ["python", "b.py"], "abc", None, None, []),
    ]
    for draft in drafts:
        # The agent draft's REQUIRED placeholder is a valid string; validation is structural.
        validate_spec(_write(tmp_path, draft))


def test_spec_json_without_extensions_is_unchanged(tmp_path: Path) -> None:
    """The registry stores model_dump_json(); no new key may appear for old specs."""
    spec = validate_spec(_write(tmp_path, _EVAL))
    assert "extensions" not in spec.model_dump_json()
    assert "extensions" not in spec.model_dump()
    with_ext = validate_spec(_write(tmp_path, _EVAL + "extensions:\n  com.example.k: v\n"))
    assert '"extensions":{"com.example.k":"v"}' in with_ext.model_dump_json()
