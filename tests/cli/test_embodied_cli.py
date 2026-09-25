"""``nova embodied odd show`` / ``nova embodied trajectory verify`` (ADR-0162 P2).

Exit-code contract (module docstring of ``novafabric.cli.embodied``):
0 = did its job, 1 = recorded evidence is defective, 2 = nothing could be
checked. Every output carries the in-mission-boundary line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.embodied import IN_MISSION_BOUNDARY

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "embodied"
BOUNDARY_START = "NovaFabric records, verifies and exports"

runner = CliRunner()


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    raw.pop("_comment", None)
    return raw


def _capsule(tmp_path: Path, embodied: dict[str, Any] | None, name: str = "run-1") -> Path:
    capsule_dir = tmp_path / name
    capsule_dir.mkdir()
    manifest: dict[str, Any] = {"run_id": name, "status": "success"}
    if embodied is not None:
        manifest["facets"] = {"embodied": embodied}
    (capsule_dir / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return capsule_dir


def _run(*args: str) -> Any:
    return runner.invoke(app, ["embodied", *args])


def _flat(text: str) -> str:
    return " ".join(text.split())


# ── help ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [["--help"], ["odd", "--help"], ["odd", "show", "--help"], ["trajectory", "verify", "--help"]],
)
def test_help_smoke(args: list[str]) -> None:
    result = _run(*args)
    assert result.exit_code == 0, result.output


# ── odd show ─────────────────────────────────────────────────────────────


def test_odd_show_renders_excursions_and_null_verdict(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-odd-trajectory-facet.json"))
    result = _run("odd", "show", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "excursions: 2" in out
    assert "fog: visibility<40m" in out and "(in_odd: false)" in out
    assert "verdict: null" in out
    assert BOUNDARY_START in out


def test_odd_show_json(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-odd-trajectory-facet.json"))
    result = _run("odd", "show", "--capsule", str(capsule), "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["recorded"] is True
    assert payload["odd"]["verdict"] is None
    assert payload["excursion_count"] == 2
    assert payload["boundary"] == IN_MISSION_BOUNDARY


def test_odd_show_with_zero_excursions_says_it_is_not_a_finding(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, {"odd": {"odd_ref": f"sha256:{'c' * 64}", "verdict": None}})
    result = _run("odd", "show", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    assert "not a finding that the system stayed inside" in _flat(result.output)


@pytest.mark.parametrize("embodied", [None, {"sensors": []}])
def test_odd_show_absent_is_reported_not_failed(tmp_path: Path, embodied: Any) -> None:
    capsule = _capsule(tmp_path, embodied)
    result = _run("odd", "show", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    assert "No ODD record" in result.output
    as_json = _run("odd", "show", "--capsule", str(capsule), "--json")
    assert json.loads(as_json.output)["recorded"] is False


def test_odd_show_non_null_verdict_exits_one(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("odd-nonnull-verdict-facet.json"))
    result = _run("odd", "show", "--capsule", str(capsule))
    assert result.exit_code == 1
    assert "AdjudicationRefusedError" in _flat(result.output)
    assert BOUNDARY_START in _flat(result.output)


def test_odd_show_non_null_verdict_json(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("odd-nonnull-verdict-facet.json"))
    result = _run("odd", "show", "--capsule", str(capsule), "--json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["error"] == "AdjudicationRefusedError"


def test_odd_show_malformed_block_exits_one(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, {"odd": {"odd_ref": "not-a-digest"}})
    assert _run("odd", "show", "--capsule", str(capsule)).exit_code == 1


def test_odd_show_resolves_a_bare_run_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _capsule(tmp_path, _fixture("valid-odd-trajectory-facet.json"), name="01HXAY")
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path))
    result = _run("odd", "show", "--capsule", "01HXAY")
    assert result.exit_code == 0, result.output


def test_missing_capsule_exits_two(tmp_path: Path) -> None:
    result = _run("odd", "show", "--capsule", str(tmp_path / "nope" / "x"))
    assert result.exit_code == 2


def test_unreadable_manifest_exits_two(tmp_path: Path) -> None:
    capsule = tmp_path / "bad"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("key: [unclosed", encoding="utf-8")
    assert _run("odd", "show", "--capsule", str(capsule)).exit_code == 2


def test_non_mapping_manifest_exits_two(tmp_path: Path) -> None:
    capsule = tmp_path / "list"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("- a\n- b\n", encoding="utf-8")
    assert _run("trajectory", "verify", "--capsule", str(capsule)).exit_code == 2


# ── trajectory verify ────────────────────────────────────────────────────


def test_trajectory_verify_intact_chain_exits_zero(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-odd-trajectory-facet.json"))
    result = _run("trajectory", "verify", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "4 hop(s) — OK" in out
    assert "acyclic: True" in out and "no_broken_parent: True" in out
    assert "re-derived nothing" in out
    assert BOUNDARY_START in out


def test_trajectory_verify_broken_parent_exits_one_naming_the_hop(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("trajectory-broken-parent-facet.json"))
    result = _run("trajectory", "verify", "--capsule", str(capsule))
    assert result.exit_code == 1
    out = _flat(result.output)
    assert "BROKEN" in out and "broken_parent (hop 2)" in out
    assert BOUNDARY_START in out


def test_trajectory_verify_json(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("trajectory-broken-parent-facet.json"))
    result = _run("trajectory", "verify", "--capsule", str(capsule), "--json")
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["no_broken_parent"] is False
    assert payload["findings"][0]["hop_index"] == 2
    assert payload["boundary"] == IN_MISSION_BOUNDARY


def test_trajectory_verify_warning_only_exits_zero(tmp_path: Path) -> None:
    raw = _fixture("valid-odd-trajectory-facet.json")
    hops = raw["trajectory"]
    hops.append(
        {
            "stage": "world_model",
            "input_digest": hops[3]["output_digest"],
            "output_digest": f"sha256:{'e' * 64}",
            "parent": hops[3]["output_digest"],
            "ts": "2026-07-13T10:02:12Z",
        }
    )
    capsule = _capsule(tmp_path, {"trajectory": hops})
    result = _run("trajectory", "verify", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    assert "WARNING stage_regression" in _flat(result.output)


@pytest.mark.parametrize("embodied", [None, {"odd": {"odd_ref": f"sha256:{'c' * 64}"}}])
def test_trajectory_verify_absent_is_not_a_pass(tmp_path: Path, embodied: Any) -> None:
    capsule = _capsule(tmp_path, embodied)
    result = _run("trajectory", "verify", "--capsule", str(capsule))
    assert result.exit_code == 2
    assert "absent is not a pass" in _flat(result.output)
    as_json = _run("trajectory", "verify", "--capsule", str(capsule), "--json")
    assert as_json.exit_code == 2
    assert json.loads(as_json.output) == {
        "recorded": False,
        "ok": False,
        "boundary": IN_MISSION_BOUNDARY,
    }


@pytest.mark.parametrize(
    "trajectory",
    [
        {"stage": "perception"},
        [{"stage": "perception", "input_digest": "x", "output_digest": "y", "ts": "t"}],
        [{"stage": "teleport", "input_digest": f"sha256:{'a' * 64}"}],
    ],
)
def test_trajectory_verify_malformed_hops_exit_one(tmp_path: Path, trajectory: Any) -> None:
    capsule = _capsule(tmp_path, {"trajectory": trajectory})
    result = _run("trajectory", "verify", "--capsule", str(capsule), "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["ok"] is False


def test_empty_trajectory_exits_one(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, {"trajectory": []})
    result = _run("trajectory", "verify", "--capsule", str(capsule))
    assert result.exit_code == 1
    assert "empty_chain" in _flat(result.output)
