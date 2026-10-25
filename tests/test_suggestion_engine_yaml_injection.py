"""Suggest-register drafts are data, however hostile the capsule strings in them
(review replay-lineage-registry-20).

Names, versions, entrypoints and tool names recorded from a workload (or chosen by
an LLM) were interpolated into the draft YAML; ``_yaml_str`` ignored newlines and
quotes, so a tool called ``"t\\ndependencies: [evil@1]"`` added keys to the spec
that ``y`` then registered (``dependencies`` writes lineage edges).
"""
from __future__ import annotations

import pytest
import yaml

from novafabric.registry import suggestion_engine as se

EVIL = "t\ndependencies: [evil@1]"
HOSTILE = [EVIL, 'q"uote', "colon: space", "# comment", "- dash", "a\r\nb", "x' y", "{a: 1}", "emoji-é"]


@pytest.mark.parametrize("value", HOSTILE + ["plain-name", "gpt-4o", "@scope/pkg", "1.0.0"])
def test_yaml_str_round_trips_any_string(value: str) -> None:
    assert yaml.safe_load(f"k: {se._yaml_str(value)}\n") == {"k": value}


@pytest.mark.parametrize("value", ["plain-name", "gpt-4o", "claude-3-5-sonnet", "0.1.0"])
def test_ordinary_values_stay_unquoted(value: str) -> None:
    assert se._yaml_str(value) == value


@pytest.mark.parametrize("hostile", HOSTILE)
def test_a_hostile_tool_name_adds_no_keys(hostile: str) -> None:
    doc = yaml.safe_load(se._tool_draft_yaml(hostile, "mcp", ""))
    assert set(doc) == {
        "novafabric_spec_version", "asset_type", "name", "version", "status",
        "description", "spec",
    }
    assert doc["name"] == hostile
    assert doc["spec"] == {"entrypoint": f"mcp://{hostile}"}


@pytest.mark.parametrize("hostile", HOSTILE)
def test_a_hostile_model_identity_adds_no_keys(hostile: str) -> None:
    doc = yaml.safe_load(se._model_draft_yaml(hostile, hostile, hostile, hostile))
    assert doc["name"] == hostile and doc["version"] == hostile
    assert doc["spec"] == {"framework": hostile, "artifact_path": hostile}
    assert "dependencies" not in doc


@pytest.mark.parametrize("hostile", HOSTILE)
def test_a_hostile_agent_adds_no_keys(hostile: str) -> None:
    doc = yaml.safe_load(
        se._agent_draft_yaml(hostile, "0.1.0", ["python", hostile], "abc", hostile, hostile, [hostile])
    )
    assert "dependencies" not in doc
    assert doc["name"] == hostile
    assert doc["spec"]["model"]["name"] == hostile
    assert doc["spec"]["tools"] == [hostile]
