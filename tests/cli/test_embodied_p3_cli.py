"""``nova embodied sim2real show|verify`` / ``teleop list`` / ``timing show`` (ADR-0162 P3).

Exit-code contract (module docstring of ``novafabric.cli.embodied``):
0 = did its job, 1 = recorded evidence is defective or contradicted,
2 = nothing could be checked. Every output carries the in-mission-boundary line.
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
RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"
POLICY = b"golden sim policy checkpoint v1"

runner = CliRunner()


def _fixture(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    raw.pop("_comment", None)
    return raw


def _capsule(
    tmp_path: Path,
    embodied: dict[str, Any] | None,
    *,
    run_id: str | None = RUN_ID,
    extra_facets: dict[str, Any] | None = None,
) -> Path:
    capsule_dir = tmp_path / "cap"
    capsule_dir.mkdir(parents=True)
    manifest: dict[str, Any] = {"status": "success"}
    if run_id is not None:
        manifest["run_id"] = run_id
    facets: dict[str, Any] = dict(extra_facets or {})
    if embodied is not None:
        facets["embodied"] = embodied
    if facets:
        manifest["facets"] = facets
    (capsule_dir / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return capsule_dir


def _run(*args: str) -> Any:
    return runner.invoke(app, ["embodied", *args])


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize(
    "args",
    [
        ["sim2real", "--help"],
        ["sim2real", "show", "--help"],
        ["sim2real", "verify", "--help"],
        ["teleop", "list", "--help"],
        ["timing", "show", "--help"],
    ],
)
def test_help_smoke(args: list[str]) -> None:
    assert _run(*args).exit_code == 0


# ── sim2real show ─────────────────────────────────────────────────────────


def test_sim2real_show(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"))
    result = _run("sim2real", "show", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert f"deployment_run_id: run:{RUN_ID}" in out and "unbound: false" in out
    assert BOUNDARY_START in out


def test_sim2real_show_unbound_json(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("sim2real-unbound-facet.json"))
    human = _run("sim2real", "show", "--capsule", str(capsule))
    assert human.exit_code == 0 and "recorded, not fatal" in _flat(human.output)
    result = _run("sim2real", "show", "--capsule", str(capsule), "--json")
    payload = json.loads(result.output)
    assert payload["sim2real"]["unbound"] is True and "sim_policy_ref" not in payload["sim2real"]
    assert payload["boundary"] == IN_MISSION_BOUNDARY


def test_sim2real_show_absent_and_malformed(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, None)
    assert _run("sim2real", "show", "--capsule", str(capsule)).exit_code == 0
    assert json.loads(_run("sim2real", "show", "--capsule", str(capsule), "--json").output) == {
        "recorded": False,
        "boundary": IN_MISSION_BOUNDARY,
    }
    bad = _capsule(tmp_path / "b", {"sim2real": {"sim_env_ref": "nope", "deployment_run_id": "r"}})
    result = _run("sim2real", "show", "--capsule", str(bad), "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "InvalidReferenceError"


# ── sim2real verify ───────────────────────────────────────────────────────


def test_sim2real_verify_passes_with_matching_policy_file(tmp_path: Path) -> None:
    raw = _fixture("valid-p3-sim2real-teleop-timing-facet.json")
    chain = {"checkpoint_chain": [{"checkpoint_digest": raw["sim2real"]["sim_policy_ref"]}, "x"]}
    capsule = _capsule(tmp_path, raw, extra_facets={"model_provenance": chain})
    policy = tmp_path / "policy.ckpt"
    policy.write_bytes(POLICY)
    result = _run("sim2real", "verify", "--capsule", str(capsule), "--policy", str(policy))
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "sim2real: OK" in out and "checkpoint_chain_bound: true" in out
    assert "artifact_resolved" in out and BOUNDARY_START in out


def test_sim2real_verify_mismatched_policy_exits_one(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"))
    forged = tmp_path / "forged.ckpt"
    forged.write_bytes(b"a different checkpoint")
    result = _run(
        "sim2real", "verify", "--capsule", str(capsule), "--policy", str(forged), "--json"
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["ok"] is False and payload["resolved"] == {"sim_policy_ref": False}
    assert any(f["code"] == "artifact_mismatch" for f in payload["findings"])


def test_sim2real_verify_deployment_mismatch_exits_one(tmp_path: Path) -> None:
    capsule = _capsule(
        tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"), run_id="other"
    )
    result = _run("sim2real", "verify", "--capsule", str(capsule))
    assert result.exit_code == 1
    assert "CONTRADICTED" in result.output and "deployment_mismatch" in _flat(result.output)


def test_sim2real_verify_unbound_is_not_fatal(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("sim2real-unbound-facet.json"), run_id=None)
    result = _run("sim2real", "verify", "--capsule", str(capsule), "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["unbound"] is True and payload["deployment_matches"] is None
    assert {f["code"] for f in payload["findings"]} == {"unbound", "deployment_unchecked"}


def test_sim2real_verify_absent_exits_two(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, {"sensors": []})
    result = _run("sim2real", "verify", "--capsule", str(capsule))
    assert result.exit_code == 2 and "absent is not a pass" in _flat(result.output)
    as_json = _run("sim2real", "verify", "--capsule", str(capsule), "--json")
    assert as_json.exit_code == 2
    assert json.loads(as_json.output)["ok"] is False


def test_sim2real_verify_unreadable_artifact_exits_two(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"))
    result = _run(
        "sim2real", "verify", "--capsule", str(capsule), "--env", str(tmp_path / "missing")
    )
    assert result.exit_code == 2


def test_sim2real_verify_malformed_block_exits_one(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, {"sim2real": ["not", "a", "mapping"]})
    assert _run("sim2real", "verify", "--capsule", str(capsule)).exit_code == 1


# ── teleop list ───────────────────────────────────────────────────────────


def test_teleop_list(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"))
    result = _run("teleop", "list", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "2 recorded, 2 shown" in out
    assert "autonomy_to_human operator=operator:fp:3f9a1c2e7b4d8a06 trigger=odd_exit" in out
    assert "latency_ms=412.5" in out and BOUNDARY_START in out


def test_teleop_list_direction_filter_json(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"))
    result = _run(
        "teleop", "list", "--capsule", str(capsule), "--direction", "human_to_autonomy", "--json"
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["handoff_count"] == 2 and payload["shown"] == 1
    assert payload["handoffs"][0]["index"] == 1 and payload["findings"] == []


def test_teleop_list_bad_direction_exits_two(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"))
    assert _run("teleop", "list", "--capsule", str(capsule), "--direction", "up").exit_code == 2


def test_teleop_list_pii_operator_exits_one_without_echo(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("teleop-pii-operator-facet.json"))
    result = _run("teleop", "list", "--capsule", str(capsule))
    assert result.exit_code == 1
    assert "OperatorIdentityError" in _flat(result.output)
    assert "jane.doe@example.com" not in result.output
    assert BOUNDARY_START in _flat(result.output)


def test_teleop_list_repeat_direction_warns(tmp_path: Path) -> None:
    raw = _fixture("valid-p3-sim2real-teleop-timing-facet.json")
    raw["teleop"][1]["direction"] = "autonomy_to_human"
    capsule = _capsule(tmp_path, {"teleop": raw["teleop"]})
    result = _run("teleop", "list", "--capsule", str(capsule))
    assert result.exit_code == 0
    assert "WARNING direction_repeat (handoff 1)" in _flat(result.output)


@pytest.mark.parametrize("teleop", [{"direction": "x"}, "nope"])
def test_teleop_list_malformed_exits_one(tmp_path: Path, teleop: Any) -> None:
    capsule = _capsule(tmp_path, {"teleop": teleop})
    assert _run("teleop", "list", "--capsule", str(capsule), "--json").exit_code == 1


def test_teleop_list_out_of_order_exits_one(tmp_path: Path) -> None:
    raw = _fixture("valid-p3-sim2real-teleop-timing-facet.json")
    capsule = _capsule(tmp_path, {"teleop": list(reversed(raw["teleop"]))})
    result = _run("teleop", "list", "--capsule", str(capsule), "--json")
    assert result.exit_code == 1 and json.loads(result.output)["error"] == "HandoffOrderError"


def test_teleop_list_absent_is_reported(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, None)
    result = _run("teleop", "list", "--capsule", str(capsule))
    assert result.exit_code == 0 and "not a finding that no human took over" in _flat(result.output)


# ── timing show ───────────────────────────────────────────────────────────


def test_timing_show(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"))
    result = _run("timing", "show", "--capsule", str(capsule))
    assert result.exit_code == 0, result.output
    out = _flat(result.output)
    assert "clock domains: 2" in out
    assert "ptp-0 source=ptp offset_ms=0.012 max_observed_latency_ms=3.4" in out
    assert "gps-0 source=gps offset_ms=unrecorded" in out
    assert "WARNING" not in out and BOUNDARY_START in out


def test_timing_show_warns_on_undescribed_sensor_domain_json(tmp_path: Path) -> None:
    raw = _fixture("valid-p3-sim2real-teleop-timing-facet.json")
    raw["timing"] = raw["timing"][:1]  # gps-0 only; sensors use ptp-0
    capsule = _capsule(tmp_path, raw)
    human = _run("timing", "show", "--capsule", str(capsule))
    assert "WARNING undeclared_clock_domain" in _flat(human.output)
    result = _run("timing", "show", "--capsule", str(capsule), "--json")
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert [f["clock_domain"] for f in payload["findings"]] == ["ptp-0"]


def test_timing_show_duplicate_domain_exits_one(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, _fixture("timing-duplicate-domain-facet.json"))
    result = _run("timing", "show", "--capsule", str(capsule), "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)["error"] == "ClockDomainConflictError"


@pytest.mark.parametrize(
    "timing", [{"clock_domain": "x"}, [{"clock_domain": "p", "source": "ntp"}]]
)
def test_timing_show_malformed_exits_one(tmp_path: Path, timing: Any) -> None:
    capsule = _capsule(tmp_path, {"timing": timing})
    assert _run("timing", "show", "--capsule", str(capsule)).exit_code == 1


def test_timing_show_absent_is_reported(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, {"sensors": []})
    result = _run("timing", "show", "--capsule", str(capsule), "--json")
    assert result.exit_code == 0 and json.loads(result.output)["recorded"] is False


# ── manifest bounds ───────────────────────────────────────────────────────


def test_oversize_manifest_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import novafabric.cli.embodied as mod

    capsule = _capsule(tmp_path, _fixture("valid-p3-sim2real-teleop-timing-facet.json"))
    monkeypatch.setattr(mod, "_MAX_MANIFEST_BYTES", 10)
    result = _run("timing", "show", "--capsule", str(capsule))
    assert result.exit_code == 2 and "cap" in result.output


def test_non_utf8_manifest_exits_two(tmp_path: Path) -> None:
    capsule = tmp_path / "bin"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_bytes(b"\xff\xfe\x00bad")
    assert _run("teleop", "list", "--capsule", str(capsule)).exit_code == 2
