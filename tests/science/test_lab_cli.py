"""CLI: ``nova science lab show|verify`` and ``nova science instrument show`` (ADR-0164 P3).

Covers the exit-code contract (0 ok / 1 failed-or-malformed / 2 usage), the
boundary line on every human output, JSON output, and ``--help`` smoke.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "science-provenance"
BOUNDARY = "never reads instrument telemetry"


def _invoke(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    return result.exit_code, result.output


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture
def capsule_from(tmp_path: Path) -> Callable[..., Path]:
    """Write a golden fixture (or a dict) as ``capsule.yaml`` in a fresh dir."""
    counter = {"n": 0}

    def _make(source: str | dict[str, Any]) -> Path:
        counter["n"] += 1
        data = json.loads((FIXTURES / source).read_text()) if isinstance(source, str) else source
        capsule = tmp_path / f"c{counter['n']}"
        capsule.mkdir()
        (capsule / "capsule.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
        return capsule

    return _make


@pytest.mark.parametrize(
    "cmd",
    [
        ["science", "lab", "--help"],
        ["science", "lab", "show", "--help"],
        ["science", "lab", "verify", "--help"],
        ["science", "instrument", "--help"],
        ["science", "instrument", "show", "--help"],
    ],
)
def test_help_smoke(cmd: list[str]) -> None:
    code, out = _invoke(*cmd)
    assert code == 0 and "Usage" in out


# ── lab show ───────────────────────────────────────────────────────────────


def test_lab_show_text(capsule_from: Callable[..., Path]) -> None:
    capsule = capsule_from("valid-lab-experiment.json")
    code, out = _invoke("science", "lab", "show", "--capsule", str(capsule))
    flat = _flat(out)
    assert code == 0
    assert "lab_kind: self_driving" in flat and "sim_to_real: real" in flat
    assert "instruments recorded: 2" in flat and BOUNDARY in flat


def test_lab_show_json(capsule_from: Callable[..., Path]) -> None:
    capsule = capsule_from("valid-lab-experiment.json")
    code, out = _invoke("science", "lab", "show", "--capsule", str(capsule), "--json")
    body = json.loads(out)
    assert code == 0
    assert body["lab_experiment"]["lab_kind"] == "self_driving"
    assert len(body["instrument_provenance"]) == 2
    assert body["controls_lab"] is False and body["reads_telemetry"] is False


def test_lab_show_absent(capsule_from: Callable[..., Path]) -> None:
    capsule = capsule_from("valid-non-science-capsule.json")
    code, out = _invoke("science", "lab", "show", "--capsule", str(capsule))
    assert code == 0 and "No lab_experiment" in out and BOUNDARY in _flat(out)
    code, out = _invoke("science", "lab", "show", "--capsule", str(capsule), "--json")
    assert code == 0 and json.loads(out)["lab_experiment"] is None


def test_lab_show_optional_fields_absent(capsule_from: Callable[..., Path]) -> None:
    data = json.loads((FIXTURES / "valid-lab-experiment.json").read_text())
    exp = data["facets"]["science_provenance"]["lab_experiment"]
    del exp["started_at"], exp["node_ref"]
    capsule = capsule_from(data)
    code, out = _invoke("science", "lab", "show", "--capsule", str(capsule))
    assert code == 0 and "(not declared)" in out and "(none)" in out
    # The digest no longer re-derives (fields removed), but node_ref is "not applicable".
    code, out = _invoke("science", "lab", "verify", "--capsule", str(capsule))
    assert code == 1 and "lineage_node_resolves: not applicable" in _flat(out)


# ── lab verify ─────────────────────────────────────────────────────────────


def test_lab_verify_ok(capsule_from: Callable[..., Path]) -> None:
    capsule = capsule_from("valid-lab-experiment.json")
    code, out = _invoke("science", "lab", "verify", "--capsule", str(capsule))
    flat = _flat(out)
    assert code == 0, out
    assert "verdict: null" in flat and BOUNDARY in flat
    assert "FAIL" not in flat


def test_lab_verify_json_ok(capsule_from: Callable[..., Path]) -> None:
    capsule = capsule_from("valid-lab-experiment.json")
    code, out = _invoke("science", "lab", "verify", "--capsule", str(capsule), "--json")
    body = json.loads(out)
    assert code == 0 and body["ok"] is True and body["verdict"] is None


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("invalid-lab-unresolved-instrument.json", "unresolved: sha256:"),
        ("invalid-lab-calibration-after-experiment.json", "calibrated after experiment: xrd-2"),
        ("invalid-lab-tampered-instrument.json", "tampered: xrd-2"),
    ],
)
def test_lab_verify_failures_exit_1(
    capsule_from: Callable[..., Path], name: str, needle: str
) -> None:
    capsule = capsule_from(name)
    code, out = _invoke("science", "lab", "verify", "--capsule", str(capsule))
    flat = _flat(out)
    assert code == 1 and "FAIL" in flat and needle in flat and BOUNDARY in flat
    code, out = _invoke("science", "lab", "verify", "--capsule", str(capsule), "--json")
    assert code == 1 and json.loads(out)["ok"] is False


def test_lab_verify_reports_unreferenced_and_unknown_time(
    capsule_from: Callable[..., Path],
) -> None:
    data = json.loads((FIXTURES / "valid-lab-experiment.json").read_text())
    science = data["facets"]["science_provenance"]
    extra = dict(science["instrument_provenance"][0])
    extra["instrument_id"] = "spare"  # unreferenced; its digest no longer re-derives
    extra["record_digest"] = "sha256:" + "f" * 64
    science["instrument_provenance"].append(extra)
    del science["lab_experiment"]["started_at"]
    del data["created_at"]
    capsule = capsule_from(data)
    code, out = _invoke("science", "lab", "verify", "--capsule", str(capsule))
    flat = _flat(out)
    assert code == 1
    assert "unreferenced instruments (informational): spare" in flat
    assert "experiment_time: unknown" in flat


@pytest.mark.parametrize(
    "name",
    [
        "invalid-lab-unknown-kind.json",
        "invalid-lab-telemetry.json",
        "invalid-lab-malformed-digest.json",
    ],
)
def test_malformed_blocks_exit_1_without_echoing_values(
    capsule_from: Callable[..., Path], name: str
) -> None:
    capsule = capsule_from(name)
    for cmd in (["lab", "verify"], ["lab", "show"], ["instrument", "show"]):
        code, out = _invoke("science", *cmd, "--capsule", str(capsule))
        assert code == 1, (cmd, out)
        assert "0.12" not in out  # the telemetry values never reach the terminal


def test_lab_verify_absent_exits_1(capsule_from: Callable[..., Path]) -> None:
    capsule = capsule_from("valid-non-science-capsule.json")
    code, out = _invoke("science", "lab", "verify", "--capsule", str(capsule))
    assert code == 1 and "No lab_experiment" in out and BOUNDARY in _flat(out)
    code, out = _invoke("science", "lab", "verify", "--capsule", str(capsule), "--json")
    assert code == 1 and json.loads(out) == {"lab_experiment": None, "verdict": None}


def test_unknown_capsule_exits_2(tmp_path: Path) -> None:
    for cmd in (["lab", "verify"], ["lab", "show"], ["instrument", "show"]):
        code, _ = _invoke("science", *cmd, "--capsule", str(tmp_path / "nope" / "x"))
        assert code == 2


# ── instrument show ────────────────────────────────────────────────────────


def test_instrument_show_text_and_json(capsule_from: Callable[..., Path]) -> None:
    capsule = capsule_from("valid-lab-experiment.json")
    code, out = _invoke("science", "instrument", "show", "--capsule", str(capsule))
    flat = _flat(out)
    assert code == 0
    assert "hplc-7 (hplc)" in flat and "xrd-2 (x-ray-diffractometer)" in flat
    assert "calibration_timestamp: 2026-06-30T09:00:00Z" in flat and BOUNDARY in flat
    code, out = _invoke("science", "instrument", "show", "--capsule", str(capsule), "--json")
    body = json.loads(out)
    assert code == 0 and body["reads_telemetry"] is False
    assert {r["instrument_id"] for r in body["instrument_provenance"]} == {"hplc-7", "xrd-2"}


def test_instrument_show_absent(capsule_from: Callable[..., Path]) -> None:
    capsule = capsule_from("valid-non-science-capsule.json")
    code, out = _invoke("science", "instrument", "show", "--capsule", str(capsule))
    assert code == 0 and "No instrument_provenance" in out
    code, out = _invoke("science", "instrument", "show", "--capsule", str(capsule), "--json")
    assert code == 0 and json.loads(out)["instrument_provenance"] == []
