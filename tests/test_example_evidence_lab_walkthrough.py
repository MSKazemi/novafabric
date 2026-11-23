"""Tests for examples/evidence_lab — EvidenceLab D2 (issue #288): the two-variant walkthrough.

D2 adds a corrected variant (a corrective prompt against the same observations), a
fresh registry with one registered input asset, real ``nova diff`` / ``nova lineage`` /
``nova verify`` / ``nova replay --mode forensic`` calls, and four separate verdict axes.

Layers, cheapest first:

* fixture pins — literals derived once with a stdlib-only script (json + hashlib), never
  read back from the code under test;
* the judges in ``walkthrough.py`` — every FAIL path is driven with a broken input;
* real runs — the harness end to end, zero-key and signed-offline, plus an independent
  re-verification of the sealed output by this test, not by the harness.
"""

from __future__ import annotations

import hashlib
import http.client
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
LAB = REPO / "examples" / "evidence_lab"
SCENARIO_DIR = LAB / "scenarios" / "service_incident_v1"
BIN_DIR = Path(sys.executable).parent
NOVA = BIN_DIR / "nova"
REAL_LOOKING_KEY = "sk-proj-REAL-LOOKING-KEY-MUST-NOT-LEAK-0123456789"

# Independently derived pins (stdlib json + hashlib straight from the files).
CORRECTED_FILE_SHA256 = {
    "prompt-corrected.txt": "65d1ba14c072898f300f9041a05ebe6a748d353659862dcb89e223d6aa341d19",
    "model_response-corrected.json": (
        "fd4022ac617092e310ca18c15b0f7e88f898362f8bb17f21173a50c1010c2a26"
    ),
    "golden/model_request-corrected.json": (
        "61d8d6b3acfa0bed6e4f25fdd923e2cdd51215abaa2957e7365b7b8a69c35be2"
    ),
}
REQUEST_SHA256 = {
    "unsafe": "a2d3ab9c3f24d3a9961a44ec3349a5fa3fe241073ab6fdaf955f2804efab0226",
    "corrected": "1cd0f95a1b0c463f2abf2c9386534758b348373c815d23ba7b510512668d5948",
}
DECISION_SHA256 = {
    "unsafe": "2e810ef917948e013ee4b0e17deecc61c6908fbca47eefb2f29fe2d6ecd9184c",
    "corrected": "0654b0be3da6c09992061676a4d0cb97e8a39addce4660a3ced9d5ed56c0c336",
}
ASSET_REF = "evidencelab-service-incident@1.0.0"

pytestmark = pytest.mark.skipif(not LAB.is_dir(), reason="examples/evidence_lab missing")


def _import_lab() -> tuple[Any, Any, Any, Any]:
    sys.path.insert(0, str(LAB))
    try:
        lab_mod = importlib.import_module("evidence_scenario")
        wt_mod = importlib.import_module("walkthrough")
        loaded = []
        for name, filename in (("evidence_lab_mock_d2", "mock_llm_server.py"),
                               ("evidence_lab_run_d2", "run.py")):  # fmt: skip
            spec = importlib.util.spec_from_file_location(name, LAB / filename)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            loaded.append(module)
        return lab_mod, wt_mod, loaded[0], loaded[1]
    finally:
        sys.path.remove(str(LAB))


lab, wt, mock, run = _import_lab()


def _canonical_sha(value: Any) -> str:  # deliberately not lab.canonical_json
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _copy_scenario(tmp_path: Path) -> Path:
    dest = tmp_path / "scenarios" / "service_incident_v1"
    shutil.copytree(SCENARIO_DIR, dest)
    return dest


def _rewrite_manifest(scenario_dir: Path, mutate: Any) -> None:
    path = scenario_dir / "scenario.yaml"
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


# ── fixtures of the corrected variant ────────────────────────────────────────


def test_corrected_fixtures_are_byte_pinned() -> None:
    for relative, digest in CORRECTED_FILE_SHA256.items():
        assert hashlib.sha256((SCENARIO_DIR / relative).read_bytes()).hexdigest() == digest


def test_each_variant_builds_its_committed_golden_request() -> None:
    for variant, golden in (("unsafe", "golden/model_request.json"),
                            ("corrected", "golden/model_request-corrected.json")):  # fmt: skip
        scn = lab.load_scenario(SCENARIO_DIR, variant)
        body = lab.build_request_body(scn, lab.expected_observations(scn))
        committed = json.loads((SCENARIO_DIR / golden).read_text())
        assert _canonical_sha(body) == _canonical_sha(committed) == REQUEST_SHA256[variant]


def test_the_variants_share_observations_and_differ_only_in_the_prompt() -> None:
    """Same effective observations; a different prompt and revision — not a mode header."""
    a = json.loads((SCENARIO_DIR / "golden/model_request.json").read_text())
    b = json.loads((SCENARIO_DIR / "golden/model_request-corrected.json").read_text())
    ua, ub = json.loads(a["messages"][1]["content"]), json.loads(b["messages"][1]["content"])
    assert ua["observations"] == ub["observations"]
    assert (ua["prompt_revision"], ub["prompt_revision"]) == ("prompt-v1", "prompt-v2-corrective")
    assert a["messages"][0]["content"] != b["messages"][0]["content"]
    assert {k: v for k, v in a.items() if k != "messages"} == {
        k: v for k, v in b.items() if k != "messages"
    }


def test_fixture_decisions_hash_to_the_pinned_digests() -> None:
    for variant, name in (("unsafe", "model_response.json"),
                          ("corrected", "model_response-corrected.json")):  # fmt: skip
        assert (
            _canonical_sha(json.loads((SCENARIO_DIR / name).read_text()))
            == (DECISION_SHA256[variant])
        )


def test_the_corrected_decision_is_accepted_and_the_unsafe_one_rejected() -> None:
    expected = {
        "unsafe": ("reject", "forbidden_action"),
        "corrected": ("accept", "allowlisted_action"),
    }
    for variant, want in expected.items():
        scn = lab.load_scenario(SCENARIO_DIR, variant)
        verdict = lab.evaluate_policy(scn, lab.Decision.model_validate(scn.model_response))
        assert (verdict.result, verdict.rule) == want
        assert scn.expected_policy_result == want[0]


# ── loader negatives ─────────────────────────────────────────────────────────


def test_an_unknown_variant_is_named() -> None:
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_scenario(SCENARIO_DIR, "imaginary")
    assert exc.value.code == "E_VARIANT_UNKNOWN"


def test_a_scenario_without_the_corrected_variant_refuses_it(tmp_path: Path) -> None:
    scenario_dir = _copy_scenario(tmp_path)
    _rewrite_manifest(scenario_dir, lambda d: d.pop("variants"))
    assert lab.load_scenario(scenario_dir).variant == "unsafe"  # D1 manifests still load
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_scenario(scenario_dir, "corrected")
    assert exc.value.code == "E_VARIANT_UNKNOWN"


def test_a_modified_corrected_fixture_is_refused(tmp_path: Path) -> None:
    scenario_dir = _copy_scenario(tmp_path)
    with open(scenario_dir / "model_response-corrected.json", "a") as handle:
        handle.write(" ")
    lab.load_scenario(scenario_dir)  # the unsafe variant does not read it
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_scenario(scenario_dir, "corrected")
    assert exc.value.code == "E_FIXTURE_DIGEST_MISMATCH"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["variants"].update(sneaky=d["variants"]["corrected"]),
        lambda d: d["variants"]["corrected"].update(surprise=1),
        lambda d: d["variants"]["corrected"].update(prompt="../prompt.txt"),
        lambda d: d["variants"]["corrected"]["fixture_sha256"].pop("prompt-corrected.txt"),
        lambda d: d["policy"]["value_bounds"].update(disable_rate_limiting={"min": 0, "max": 1}),
        lambda d: d["policy"]["value_bounds"]["reduce_max_connections"].update(min=950),
        lambda d: d["variants"]["corrected"]["expected"].update(policy_result="maybe"),
    ],
    ids=[
        "unknown-variant",
        "unknown-key",
        "path-escape",
        "unpinned-file",
        "bounds-on-forbidden",
        "min-above-max",
        "bad-expected",
    ],  # fmt: skip
)
def test_variant_and_bounds_manifest_errors_are_named(tmp_path: Path, mutate: Any) -> None:
    scenario_dir = _copy_scenario(tmp_path)
    _rewrite_manifest(scenario_dir, mutate)
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_scenario(scenario_dir, "corrected")
    assert exc.value.code == "E_MANIFEST_INVALID"


# ── the mock serves exactly its own variant's request ────────────────────────


@pytest.fixture
def served() -> Iterator[Any]:
    servers: list[Any] = []

    def start(variant: str) -> int:
        server = mock.make_server(lab.load_scenario(SCENARIO_DIR, variant), request_timeout=5.0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return int(server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _post(port: int, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    raw = json.dumps(body).encode()
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("POST", "/v1/chat/completions", raw, {"Content-Type": "application/json"})
    response = conn.getresponse()
    payload = json.loads(response.read())
    conn.close()
    return response.status, payload


def test_each_mock_serves_only_its_own_variants_request(served: Any) -> None:
    golden = {v: json.loads((SCENARIO_DIR / g).read_text()) for v, g in
              (("unsafe", "golden/model_request.json"),
               ("corrected", "golden/model_request-corrected.json"))}  # fmt: skip
    for variant, other in (("unsafe", "corrected"), ("corrected", "unsafe")):
        port = served(variant)
        status, payload = _post(port, golden[other])
        assert status == 400 and "choices" not in payload
        assert payload["error"]["code"] == "E_PROMPT_MISMATCH"
        status, payload = _post(port, golden[variant])
        assert status == 200
        decision = json.loads(payload["choices"][0]["message"]["content"])
        assert _canonical_sha(decision) == DECISION_SHA256[variant]


# ── judges: every FAIL path ──────────────────────────────────────────────────

D1_CHECKS_OK = {"one_logical_model_call": True, "two_read_only_tool_calls_in_order": True}
GOOD_OBSERVED = {"logical_model_calls": 1, "tool_calls": 2,
                 "tool_sequence": ["read_metrics", "search_logs"]}  # fmt: skip


def test_check_refuses_an_unknown_axis_or_verdict() -> None:
    with pytest.raises(ValueError):
        wt.Check("vibes", None, "n", "PASS", "r", 1, 1, "m", None, "PASS")
    with pytest.raises(ValueError):
        wt.Check("task", None, "n", "GREEN", "r", 1, 1, "m", None, "PASS")  # type: ignore[arg-type]


def test_judge_task_reports_the_oracle_verdict_and_refuses_a_missing_one() -> None:
    c = wt.judge_task(
        "unsafe", {"verdict": "FAIL", "reason": "r"}, expected="FAIL", capsule_ref="x"
    )
    assert (c.verdict, c.as_expected, c.reason_code) == ("FAIL", True, "policy-rejected")
    c = wt.judge_task("corrected", {"verdict": "FAIL"}, expected="PASS", capsule_ref="x")
    assert not c.as_expected
    c = wt.judge_task("corrected", {}, expected="PASS", capsule_ref="x")
    assert (c.verdict, c.reason_code) == ("FAIL", "task-verdict-missing")


@pytest.mark.parametrize(
    ("observed", "checks", "recorded", "reason"),
    [
        (GOOD_OBSERVED, D1_CHECKS_OK, DECISION_SHA256["corrected"], "capture-matches-fixtures"),
        (GOOD_OBSERVED, D1_CHECKS_OK, DECISION_SHA256["unsafe"], "decision-digest-drift"),
        ({**GOOD_OBSERVED, "tool_calls": 1, "tool_sequence": ["read_metrics"]}, D1_CHECKS_OK,
         DECISION_SHA256["corrected"], "capture-count-mismatch"),
        ({**GOOD_OBSERVED, "logical_model_calls": 0}, D1_CHECKS_OK,
         DECISION_SHA256["corrected"], "capture-count-mismatch"),
        (GOOD_OBSERVED, {**D1_CHECKS_OK, "tool_records_digest_only": False},
         DECISION_SHA256["corrected"], "capture-check-failed"),
        (GOOD_OBSERVED, {}, DECISION_SHA256["corrected"], "capture-check-failed"),
    ],
    ids=["ok", "decision-drift", "missing-tool", "missing-model", "d1-check-false", "no-checks"],
)  # fmt: skip
def test_judge_capture(observed: Any, checks: Any, recorded: str, reason: str) -> None:
    c = wt.judge_capture("corrected", observed, checks,
                         expected_decision_sha256=DECISION_SHA256["corrected"],
                         recorded_decision_sha256=recorded, capsule_ref="x")  # fmt: skip
    assert c.reason_code == reason
    assert c.as_expected == (reason == "capture-matches-fixtures")


def _good_diff() -> dict[str, Any]:
    pair = {"changed": True, "request_changed": True, "response_changed": True,
            "provider_changed": False, "params_changed": False, "changed_params": [],
            "model_call_id_a": "CA", "model_call_id_b": "CB"}  # fmt: skip
    tool_pairs = [{"changed": False, "tool_name": name, "tool_call_id_a": f"TA{i}",
                   "tool_call_id_b": f"TB{i}", "result_changed": False,
                   "arguments_changed": False, "status_changed": False,
                   "status_a": "success", "status_b": "success"}
                  for i, name in enumerate(("read_metrics", "search_logs"))]  # fmt: skip
    return {
        "run_a_id": "A", "run_b_id": "B", "has_changes": True,
        "skipped_malformed_lines": {"a": {"model_calls": 0, "tool_calls": 0},
                                    "b": {"model_calls": 0, "tool_calls": 0}},
        "sections": {
            "model_calls": {"aligned": 1, "changed": 1, "added": 0, "removed": 0, "pairs": [pair]},
            "tool_calls": {"aligned": 2, "changed": 0, "added": 0, "removed": 0,
                           "pairs": tool_pairs},
            "environment": {"changes": []},
            "outputs": {"changes": [{"path": "outputs/stdout.txt", "before_hash": "sha256:OA",
                                     "after_hash": "sha256:OB"}]},
        },
    }  # fmt: skip


def _tool_pairs_swapped(d: dict[str, Any]) -> None:  # counts still say "2 aligned, 0 changed"
    pairs = d["sections"]["tool_calls"]["pairs"]
    pairs[0]["tool_name"], pairs[1]["tool_name"] = "search_logs", "read_metrics"


def _tool_result_changed_in_a_pair(d: dict[str, Any]) -> None:
    d["sections"]["tool_calls"]["pairs"][1]["result_changed"] = True


def _tool_pairs_missing(d: dict[str, Any]) -> None:
    d["sections"]["tool_calls"]["pairs"] = []


def _malformed_lines_skipped(d: dict[str, Any]) -> None:
    d["skipped_malformed_lines"]["b"]["tool_calls"] = 1


def _env_changed(d: dict[str, Any]) -> None:
    d["sections"]["environment"]["changes"] = [{"key": "PATH", "before": "a", "after": "b"}]


def _output_change_missing(d: dict[str, Any]) -> None:
    d["sections"]["outputs"]["changes"] = []


def _output_hash_wrong(d: dict[str, Any]) -> None:
    d["sections"]["outputs"]["changes"][0]["after_hash"] = "sha256:SOMETHING-ELSE"


def _extra_output_changed(d: dict[str, Any]) -> None:
    d["sections"]["outputs"]["changes"].append(
        {"path": "outputs/stderr.txt", "before_hash": "sha256:1", "after_hash": "sha256:2"}
    )


def _outputs_section_missing(d: dict[str, Any]) -> None:
    del d["sections"]["outputs"]


def _tool_changed(d: dict[str, Any]) -> None:
    d["sections"]["tool_calls"].update(aligned=1, changed=1)


def _model_added(d: dict[str, Any]) -> None:
    d["sections"]["model_calls"]["added"] = 1


def _no_change(d: dict[str, Any]) -> None:
    d["has_changes"] = False


def _request_same(d: dict[str, Any]) -> None:
    d["sections"]["model_calls"]["pairs"][0]["request_changed"] = False


def _params_changed(d: dict[str, Any]) -> None:
    d["sections"]["model_calls"]["pairs"][0]["params_changed"] = True


def _swapped_runs(d: dict[str, Any]) -> None:
    d["run_a_id"], d["run_b_id"] = "B", "A"


def _wrong_records_paired(d: dict[str, Any]) -> None:
    d["sections"]["model_calls"]["pairs"][0]["model_call_id_b"] = "SOME-OTHER-CALL"


def _model_aligned(d: dict[str, Any]) -> None:
    d["sections"]["model_calls"]["aligned"] = 2


DIFF_ARGS = {"run_a": "A", "run_b": "B", "call_a": "CA", "call_b": "CB", "exit_code": 0,
             "stdout_a": "OA", "stdout_b": "OB", "artifact": None}  # fmt: skip


@pytest.mark.parametrize(
    "mutate",
    [
        _tool_changed,
        _model_added,
        _no_change,
        _request_same,
        _params_changed,
        _swapped_runs,
        _wrong_records_paired,
        _model_aligned,
        _env_changed,
        _output_change_missing,
        _output_hash_wrong,
        _extra_output_changed,
        _outputs_section_missing,
        _tool_pairs_swapped,
        _tool_result_changed_in_a_pair,
        _tool_pairs_missing,
        _malformed_lines_skipped,
    ],  # fmt: skip
)
def test_judge_diff_fails_on_any_unexpected_change_set(mutate: Any) -> None:
    good = wt.judge_diff(_good_diff(), **DIFF_ARGS)
    assert (good.verdict, good.as_expected) == ("PASS", True)
    diff = _good_diff()
    mutate(diff)
    bad = wt.judge_diff(diff, **DIFF_ARGS)
    assert (bad.verdict, bad.reason_code) == ("FAIL", "diff-mismatch")


def test_judge_diff_fails_when_nova_diff_did_not_run_or_ids_are_unknown() -> None:
    c = wt.judge_diff(None, **{**DIFF_ARGS, "exit_code": 2})
    assert (c.verdict, c.reason_code) == ("FAIL", "diff-did-not-run")
    c = wt.judge_diff(_good_diff(), **{**DIFF_ARGS, "call_b": None})
    assert (c.verdict, c.reason_code) == ("FAIL", "diff-no-call-ids")


def test_an_aborted_capture_is_a_named_capture_failure() -> None:
    c = wt.judge_capture_aborted("corrected", "E_COUNT_MISMATCH", "expected 1, got 2")
    assert (c.axis, c.variant, c.verdict, c.reason_code, c.as_expected) == (
        "capture",
        "corrected",
        "FAIL",
        "E_COUNT_MISMATCH",
        False,
    )


def _edge(run_id: str, asset: str = ASSET_REF) -> dict[str, Any]:
    return {"edge_type": "consumed", "source": {"kind": "run", "run_id": run_id},
            "target": {"kind": "asset", "asset_ref": asset}}  # fmt: skip


#: Shaped like real `nova inspect name@version` output (a rich panel), written here.
INSPECT_OK = (
    "╭──── evidencelab-service-incident@1.0.0 ────╮\n"
    "│ id: db95766f-09f6-4647-b2d6-a1677e7ad335    │\n"
    "│ name: evidencelab-service-incident          │\n"
    "│ asset_type: dataset                         │\n"
    "│ version: 1.0.0                              │\n"
    "│ status: development                         │\n"
    "╰─────────────────────────────────────────────╯\n"
)
REGISTRY_OK = {"registry_exit_code": 0, "registry_output": INSPECT_OK}


@pytest.mark.parametrize(
    ("provenance", "edges", "reason"),
    [
        ([{"kind": "asset", "ref": f"local:{ASSET_REF}"}], [_edge("R")],
         "declared-consumption-of-registered-asset"),
        ([], [_edge("R")], "lineage-empty"),
        ([{"kind": "asset", "ref": "local:something-else@1"}], [_edge("R")],
         "lineage-unexpected-asset"),
        ([{"kind": "asset", "ref": f"local:{ASSET_REF}"},
          {"kind": "asset", "ref": "local:extra@1"}], [_edge("R")], "lineage-unexpected-asset"),
        ([{"kind": "asset", "ref": f"local:{ASSET_REF}"}], [], "lineage-edge-mismatch"),
        ([{"kind": "asset", "ref": f"local:{ASSET_REF}"}], [_edge("OTHER")],
         "lineage-edge-mismatch"),
    ],
    ids=["ok", "empty", "other-asset", "extra-asset", "no-edge", "edge-of-another-run"],
)  # fmt: skip
def test_judge_lineage(provenance: Any, edges: Any, reason: str) -> None:
    c = wt.judge_lineage("unsafe", provenance, edges, run_id="R", exit_code=0, artifact=None,
                         **REGISTRY_OK)  # fmt: skip
    assert c.reason_code == reason
    assert c.as_expected == (reason == "declared-consumption-of-registered-asset")
    assert c.name == "run_declared_registered_asset"


def test_judge_lineage_fails_when_the_query_fails() -> None:
    c = wt.judge_lineage("unsafe", None, [], run_id="R", exit_code=1, artifact=None,
                         **REGISTRY_OK)  # fmt: skip
    assert (c.verdict, c.reason_code) == ("FAIL", "lineage-query-failed")


@pytest.mark.parametrize(
    ("code", "output"),
    [
        (1, "Asset 'evidencelab-service-incident@1.0.0' not found.\n"),
        (0, ""),
        (0, INSPECT_OK.replace("version: 1.0.0", "version: 9.9.9")),
        (0, INSPECT_OK.replace("name: evidencelab-service-incident", "name: other")),
        (0, INSPECT_OK.replace("asset_type: dataset", "asset_type: model")),
    ],
    ids=["not-found", "no-output", "other-version", "other-name", "other-type"],
)  # fmt: skip
def test_judge_lineage_needs_the_asset_read_back_from_the_registry(code: int, output: str) -> None:
    """A matching lineage edge alone is not enough: the edge is built from the capsule's own
    assets.jsonl and never consults the registry, so registration is read back separately."""
    c = wt.judge_lineage("unsafe", [{"kind": "asset", "ref": f"local:{ASSET_REF}"}],
                         [_edge("R")], run_id="R", exit_code=0, artifact=None,
                         registry_exit_code=code, registry_output=output)  # fmt: skip
    assert (c.verdict, c.reason_code, c.as_expected) == ("FAIL", "asset-not-in-registry", False)


def test_judge_lineage_says_consumption_is_declared_not_observed() -> None:
    edge = {**_edge("R"), "confidence": "observed"}  # what NovaFabric's writer labels it
    c = wt.judge_lineage("unsafe", [{"kind": "asset", "ref": f"local:{ASSET_REF}"}], [edge],
                         run_id="R", exit_code=0, artifact=None, **REGISTRY_OK)  # fmt: skip
    assert c.verdict == "PASS"
    assert c.observed["consumption_source"] == "declared-by-agent"
    assert c.observed["edge_confidence_label"] == ["observed"]
    assert c.observed["registry_readback"] == {"exit_code": 0, "name": ASSET_REF.split("@")[0],
                                               "version": "1.0.0", "asset_type": "dataset"}  # fmt: skip
    assert any("declared" in n and "not observed" in n for n in c.notes)


GOOD_VERIFY = {"valid": True, "sealed": True, "signature_ok": True, "capsule_binding_ok": True,
               "ca_chain_checked": True, "ca_chain_ok": True, "identity_trust": "local-ca-pinned",
               "timestamp_trust": "absent"}  # fmt: skip


@pytest.mark.parametrize(
    ("profile", "verify", "code", "verdict", "reason", "as_expected"),
    [
        ("zero-key", {"sealed": False, "valid": False}, 1, "NOT_EVALUATED", "seal-not-configured",
         True),
        ("zero-key", GOOD_VERIFY, 0, "FAIL", "unexpected-seal-state", False),
        ("zero-key", None, 2, "FAIL", "unexpected-seal-state", False),
        ("signed-offline", GOOD_VERIFY, 0, "PASS", "verified-against-pinned-local-ca", True),
        ("signed-offline", {**GOOD_VERIFY, "valid": False, "capsule_binding_ok": False}, 1,
         "FAIL", "verification-failed", False),
        ("signed-offline", {**GOOD_VERIFY, "identity_trust": "self-asserted",
                            "ca_chain_ok": False}, 1, "FAIL", "verification-failed", False),
        ("signed-offline", GOOD_VERIFY, 1, "FAIL", "verification-failed", False),
        ("signed-offline", {"sealed": False, "valid": False}, 1, "FAIL", "seal-missing", False),
    ],
    ids=["unsealed", "zero-key-but-sealed", "zero-key-no-output", "pinned-ok", "tampered",
         "wrong-ca", "nonzero-exit", "seal-missing"],
)  # fmt: skip
def test_judge_integrity(profile: str, verify: Any, code: int, verdict: str, reason: str,
                         as_expected: bool) -> None:  # fmt: skip
    c = wt.judge_integrity("unsafe", profile, verify, exit_code=code, artifact=None)
    assert (c.verdict, c.reason_code, c.as_expected) == (verdict, reason, as_expected)


#: Literal per-case signals (written here, not read from walkthrough.NEGATIVE_SIGNALS).
REFUSALS = {
    "tampered-member": {**GOOD_VERIFY, "valid": False, "capsule_binding_ok": False},
    "missing-member": {**GOOD_VERIFY, "valid": False, "capsule_binding_ok": False},
    "corrupt-signature": {**GOOD_VERIFY, "valid": False, "signature_ok": False},
    "wrong-ca": {**GOOD_VERIFY, "valid": False, "ca_chain_ok": False,
                 "identity_trust": "self-asserted"},
}  # fmt: skip


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_judge_negative_proof_needs_the_right_reason(case: str) -> None:
    refused = wt.judge_negative_proof(case, REFUSALS[case], exit_code=1, artifact=None)
    assert (refused.verdict, refused.reason_code, refused.as_expected) == (
        "FAIL",
        "verifier-refused",
        True,
    )
    # refused, but for some other reason than the one this case breaks: proves nothing
    other = next(v for k, v in REFUSALS.items() if v != REFUSALS[case])
    if all(other.get(k) == REFUSALS[case].get(k) for k in wt.NEGATIVE_SIGNALS[case]):
        other = {**GOOD_VERIFY, "valid": False}
    c = wt.judge_negative_proof(case, other, exit_code=1, artifact=None)
    assert (c.reason_code, c.as_expected) == ("refused-for-another-reason", False)
    for verify, code in ((GOOD_VERIFY, 0), (REFUSALS[case], 0), (GOOD_VERIFY, 1), (None, 1)):
        c = wt.judge_negative_proof(case, verify, exit_code=code, artifact=None)
        assert (c.reason_code, c.as_expected) == ("false-green", False), (verify, code)


#: The properties each broken copy must leave INTACT, so a refusal names one cause. Written
#: here from measured `nova verify --json` output of the four real broken copies.
INTACT = {
    "tampered-member": ("signature_ok", "ca_chain_ok"),
    "missing-member": ("signature_ok", "ca_chain_ok"),
    "corrupt-signature": ("capsule_binding_ok",),
    "wrong-ca": ("signature_ok", "capsule_binding_ok"),
}


@pytest.mark.parametrize("case", sorted(REFUSALS))
def test_a_negative_proof_refused_for_every_reason_at_once_proves_nothing(case: str) -> None:
    everything_broken = {**GOOD_VERIFY, "valid": False, "signature_ok": False,
                         "capsule_binding_ok": False, "ca_chain_ok": False}  # fmt: skip
    c = wt.judge_negative_proof(case, everything_broken, exit_code=1, artifact=None)
    assert (c.reason_code, c.as_expected) == ("refused-for-another-reason", False)
    for prop in INTACT[case]:  # break only one property that must stay intact
        c = wt.judge_negative_proof(case, {**REFUSALS[case], prop: False}, exit_code=1,
                                    artifact=None)  # fmt: skip
        assert (c.reason_code, c.as_expected) == ("refused-for-another-reason", False), prop


def test_the_pristine_copy_control_must_verify() -> None:
    ok = wt.judge_pristine_copy(GOOD_VERIFY, exit_code=0, artifact=None)
    assert (ok.variant, ok.verdict, ok.as_expected) == ("control:pristine-copy", "PASS", True)
    broken = wt.judge_pristine_copy(REFUSALS["tampered-member"], exit_code=1, artifact=None)
    assert (broken.verdict, broken.as_expected) == ("FAIL", False)
    assert not wt.proof_requirement_met([ok])  # a control is not a run's integrity verdict


def test_judge_replay_never_claims_fidelity() -> None:
    rid = "01M82C1S764P9M1SMC2PV9X6WW"
    c = wt.judge_replay("unsafe", exit_code=0, replay_id=rid, artifact=None, artifact_exists=True)
    assert (c.verdict, c.reason_code, c.as_expected) == ("NOT_EVALUATED", "forensic-only", True)
    assert c.observed["forensic_inspection_ran"] is True
    for code, replay_id, exists in ((1, rid, True), (0, None, False), (0, rid, False)):
        c = wt.judge_replay("unsafe", exit_code=code, replay_id=replay_id, artifact=None,
                            artifact_exists=exists)  # fmt: skip
        assert (c.verdict, c.reason_code, c.as_expected) == (
            "FAIL",
            "forensic-inspection-failed",
            False,
        )
        assert c.observed["forensic_inspection_ran"] is False


def test_harness_verdict_and_proof_requirement() -> None:
    def check(axis: str, verdict: str, expected: str, variant: str = "unsafe") -> Any:
        return wt.Check(axis, variant, "n", verdict, "r", 1, 1, "m", None, expected)  # type: ignore[arg-type]

    ok = [check("task", "FAIL", "FAIL"), check("integrity", "NOT_EVALUATED", "NOT_EVALUATED")]
    assert wt.harness_verdict(ok) == "PASS"
    assert wt.harness_verdict([*ok, check("task", "PASS", "FAIL")]) == "FAIL"
    assert wt.harness_verdict([]) == "FAIL"
    assert not wt.proof_requirement_met(ok)
    sealed = [check("integrity", "PASS", "PASS"), check("integrity", "PASS", "PASS", "corrected"),
              check("integrity", "FAIL", "FAIL", "negative:x")]  # fmt: skip
    assert wt.proof_requirement_met(sealed)
    assert not wt.proof_requirement_met([])


def test_proof_requirement_needs_a_verified_integrity_check_for_every_variant() -> None:
    """A run that stopped before the second variant was verified must not read as proven."""

    def check(variant: str, verdict: str = "PASS") -> Any:
        return wt.Check("integrity", variant, "native_seal_verification", verdict, "r", 1, 1,  # type: ignore[arg-type]
                        "m", None, "PASS")  # fmt: skip

    assert not wt.proof_requirement_met([check("unsafe")])
    assert not wt.proof_requirement_met([check("corrected")])
    assert not wt.proof_requirement_met([check("unsafe"), check("corrected", "FAIL")])
    assert wt.proof_requirement_met([check("unsafe"), check("corrected")])


#: Every check a complete walkthrough must report, written here as literals (axis, variant,
#: name) — not read from walkthrough.required_checks, which is under test.
_PER_VARIANT = [("task", "policy_oracle_verdict"), ("capture", "logical_calls_and_decision"),
                ("capture", "run_declared_registered_asset"),
                ("integrity", "native_seal_verification"), ("replay", "replay_fidelity")]  # fmt: skip
ZERO_KEY_CHECKS = {(a, v, n) for v in ("unsafe", "corrected") for a, n in _PER_VARIANT} | {
    ("capture", None, "nova_diff_names_the_change")
}
SIGNED_CHECKS = ZERO_KEY_CHECKS | {
    ("integrity", f"negative:{case}", "broken_copy_is_refused")
    for case in ("tampered-member", "missing-member", "corrupt-signature", "wrong-ca")
} | {("integrity", "control:pristine-copy", "untouched_copy_verifies")}


def _complete(required: set[tuple[str, str | None, str]]) -> list[Any]:
    return [wt.Check(axis, variant, name, "PASS", "r", 1, 1, "m", None, "PASS")  # type: ignore[arg-type]
            for axis, variant, name in sorted(required, key=str)]  # fmt: skip


@pytest.mark.parametrize(("profile", "required"), [("zero-key", ZERO_KEY_CHECKS),
                                                   ("signed-offline", SIGNED_CHECKS)])  # fmt: skip
def test_the_harness_verdict_needs_every_required_check(
    profile: str, required: set[tuple[str, str | None, str]]
) -> None:  # fmt: skip
    """A skipped step (say the diff never ran) must not leave a PASS behind."""
    assert set(wt.required_checks(profile)) == required
    assert (len(ZERO_KEY_CHECKS), len(SIGNED_CHECKS)) == (11, 16)
    full = _complete(required)
    assert wt.harness_verdict(full, wt.required_checks(profile)) == "PASS"
    for dropped in range(len(full)):
        partial = full[:dropped] + full[dropped + 1 :]
        assert wt.harness_verdict(partial, wt.required_checks(profile)) == "FAIL", full[dropped]
        assert wt.missing_checks(partial, wt.required_checks(profile)) == [
            (full[dropped].axis, full[dropped].variant, full[dropped].name)
        ]
    assert wt.harness_verdict([*full, full[0]], wt.required_checks(profile)) == "FAIL"  # dup


def test_a_failed_walkthrough_never_reports_the_proof_requirement_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # fmt: skip
    """Both runs verified, then a later step failed: the report must not say 'proven'."""
    pytest.importorskip("openai")

    def verified_then_failed(*_: Any, **__: Any) -> None:
        report = _[-1]
        report["checks_objects"] = [
            wt.Check("integrity", v, "native_seal_verification", "PASS", "r", 1, 1, "m", None,
                     "PASS") for v in ("unsafe", "corrected")
        ]  # fmt: skip
        raise lab.EvidenceLabError("E_SEAL_INIT_FAILED", "the wrong-CA home could not be made")

    monkeypatch.setattr(run, "_walkthrough", verified_then_failed)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    out = tmp_path / "out"
    code = run.main(["--scenario", "service-incident-v1", "--output-dir", str(out),
                     "--profile", "signed-offline", "--require-verified"])  # fmt: skip
    report = json.loads((out / "evidence-lab-report.json").read_text())
    assert code == 2 and report["status"] == "error"
    assert report["harness_verdict"] == "FAIL"
    assert report["proof_requirement_met"] is False


# ── agent and CLI guards ─────────────────────────────────────────────────────


def test_agent_refuses_to_record_consumption_outside_a_capture() -> None:
    agent = sys.modules.get("evidence_lab_agent_d2")
    if agent is None:
        sys.path.insert(0, str(LAB))
        try:
            spec = importlib.util.spec_from_file_location("evidence_lab_agent_d2", LAB / "agent.py")
            assert spec is not None and spec.loader is not None
            agent = importlib.util.module_from_spec(spec)
            sys.modules["evidence_lab_agent_d2"] = agent
            spec.loader.exec_module(agent)
        finally:
            sys.path.remove(str(LAB))
    with pytest.raises(lab.EvidenceLabError) as exc:
        agent.record_consumption(ASSET_REF, "development", {})
    assert exc.value.code == "E_CAPTURE_CONTEXT_MISSING"


@pytest.mark.parametrize("extra", [["--profile", "signed-offline"], ["--require-verified"]])
def test_single_variant_mode_refuses_walkthrough_only_flags(tmp_path: Path, extra: list[str],
                                                            capsys: pytest.CaptureFixture[str]) -> None:  # fmt: skip
    code = run.main(["--scenario", "service-incident-v1", "--variant", "unsafe",
                     "--output-dir", str(tmp_path / "o"), *extra])  # fmt: skip
    assert code == 2 and "code=E_ARGUMENT_INVALID" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()


def _fake_nova(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-nova"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return script


def test_walkthrough_names_a_failed_registration_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    pytest.importorskip("openai")
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", REAL_LOOKING_KEY)
    nova = _fake_nova(tmp_path, 'echo "Error: registry is read-only" >&2; exit 1')
    out = tmp_path / "out"
    code = run.main(["--scenario", "service-incident-v1", "--output-dir", str(out),
                     "--nova-bin", str(nova)])  # fmt: skip
    assert code == 2 and "code=E_ASSET_REGISTER_FAILED" in capsys.readouterr().err
    report = json.loads((out / "evidence-lab-report.json").read_text())
    assert report["status"] == "error" and report["harness_verdict"] == "FAIL"
    assert report["error"]["code"] == "E_ASSET_REGISTER_FAILED"
    assert report["cleanup"]["temp_home_removed"] is True
    assert os.listdir(tmp_path / "home") == []


@pytest.mark.parametrize("kind", ["missing", "fifo"])
def test_a_broken_golden_transcript_is_named_not_a_traceback_or_a_hang(
    tmp_path: Path, kind: str
) -> None:  # fmt: skip
    pytest.importorskip("openai")
    scenario_dir = _copy_scenario(tmp_path)
    golden = scenario_dir / "golden" / "model_request-corrected.json"
    golden.unlink()
    if kind == "fifo":
        os.mkfifo(golden)  # opening it for reading would block forever
    (tmp_path / "home").mkdir()
    done = subprocess.run(
        [sys.executable, str(LAB / "run.py"), "--scenario", "service-incident-v1",
         "--output-dir", str(tmp_path / "out"), "--scenarios-root", str(tmp_path / "scenarios"),
         "--nova-bin", str(_fake_nova(tmp_path, "exit 3"))],
        env={"PATH": f"{BIN_DIR}:/usr/bin:/bin", "HOME": str(tmp_path / "home")},
        capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert done.returncode == 2, done.stderr[-2000:]
    assert "Traceback" not in done.stderr
    code = {"missing": "E_FIXTURE_MISSING", "fifo": "E_FIXTURE_INVALID"}[kind]
    assert f"code={code}" in done.stderr


# ── real runs ────────────────────────────────────────────────────────────────


def needs_nova(fn: Any) -> Any:
    fn = pytest.mark.skipif(not NOVA.is_file(), reason="needs the installed nova console script")(
        fn
    )
    return pytest.mark.xdist_group("evidence-lab-d2")(fn)


def _walk(tmp_path: Path, name: str, *extra: str) -> dict[str, Any]:
    pytest.importorskip("openai")
    home = tmp_path / f"{name}-user-home"
    home.mkdir()
    env = {"PATH": f"{BIN_DIR}:/usr/bin:/bin", "HOME": str(home),
           "OPENAI_API_KEY": REAL_LOOKING_KEY, "HTTPS_PROXY": "http://10.0.0.1:3128",
           "NOVA_CAPTURE_LEVEL": "forensic"}  # fmt: skip
    out = tmp_path / name
    done = subprocess.run(
        [sys.executable, str(LAB / "run.py"), "--scenario", "service-incident-v1",
         "--output-dir", str(out), *extra],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=600,
    )  # fmt: skip
    report = json.loads((out / "evidence-lab-report.json").read_text())
    return {"done": done, "out": out, "report": report, "home": home}


@pytest.fixture(scope="module")
def zero_key(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _walk(tmp_path_factory.mktemp("d2-zero-key"), "walk")


@pytest.fixture(scope="module")
def signed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    return _walk(tmp_path_factory.mktemp("d2-signed"), "walk", "--profile", "signed-offline",
                 "--require-verified")  # fmt: skip


def _checks(report: dict[str, Any], axis: str, name: str) -> dict[str, dict[str, Any]]:
    return {c["variant"]: c for c in report["checks"] if c["axis"] == axis and c["name"] == name}


@needs_nova
def test_zero_key_walkthrough_passes_with_two_real_captures(zero_key: dict[str, Any]) -> None:
    done, report = zero_key["done"], zero_key["report"]
    assert done.returncode == 0, done.stderr[-2000:]
    assert report["status"] == "ok" and report["harness_verdict"] == "PASS"
    runs = report["runs"]
    assert set(runs) == {"unsafe", "corrected"}
    assert runs["unsafe"]["run_id"] != runs["corrected"]["run_id"]
    for variant, info in runs.items():
        assert (info["model_logical_count"], info["tool_count"]) == (1, 2)
        assert info["decision_digest"] == DECISION_SHA256[variant]
        assert info["request_sha256"] == REQUEST_SHA256[variant]
        assert info["commands"]["validate"]["exit_code"] == 0
        assert Path(info["capsule_path"]).is_dir()
    assert "a NOT_EVALUATED is not a pass" in done.stdout


@needs_nova
def test_zero_key_axes_are_separate_and_honest(zero_key: dict[str, Any]) -> None:
    report = zero_key["report"]
    task = _checks(report, "task", "policy_oracle_verdict")
    assert (task["unsafe"]["verdict"], task["corrected"]["verdict"]) == ("FAIL", "PASS")
    integrity = _checks(report, "integrity", "native_seal_verification")
    replay = _checks(report, "replay", "replay_fidelity")
    for variant in ("unsafe", "corrected"):
        # nova validate exited 0 for this run, and integrity is still NOT_EVALUATED
        assert report["runs"][variant]["commands"]["validate"]["exit_code"] == 0
        assert (integrity[variant]["verdict"], integrity[variant]["reason_code"]) == (
            "NOT_EVALUATED",
            "seal-not-configured",
        )
        assert integrity[variant]["observed"]["sealed"] is False
        assert (replay[variant]["verdict"], replay[variant]["reason_code"]) == (
            "NOT_EVALUATED",
            "forensic-only",
        )
        assert replay[variant]["observed"]["forensic_inspection_ran"] is True
    assert report["proof_requirement_met"] is False
    assert all(set(c) >= {"axis", "variant", "verdict", "reason_code", "expected", "observed",
                          "oracle_method", "artifact_reference"} for c in report["checks"])  # fmt: skip


@needs_nova
def test_zero_key_report_carries_the_contract_fields(zero_key: dict[str, Any]) -> None:
    report = zero_key["report"]
    for key in ("scenario_id", "scenario_revision", "fixture_hashes", "source_revision",
                "harness_revision", "profile", "runs", "commands", "source_assets", "checks"):  # fmt: skip
        assert report[key], key
    assert report["fixture_hashes"]["variants"]["corrected"] == {
        k: v for k, v in CORRECTED_FILE_SHA256.items() if not k.startswith("golden/")
    }
    assert report["source_assets"][0]["asset_ref"] == ASSET_REF
    assert report["commands"]["register"]["exit_code"] == 0
    assert "not a NovaFabric portable format" in report["kind"]


@needs_nova
def test_lineage_is_independently_the_registered_asset(zero_key: dict[str, Any]) -> None:
    """Read each capsule's lineage.jsonl here, without the harness's judge."""
    for info in zero_key["report"]["runs"].values():
        capsule = Path(info["capsule_path"])
        assets = [json.loads(x) for x in (capsule / "assets.jsonl").read_text().splitlines() if x]
        assert [a["asset_ref"] for a in assets] == [ASSET_REF]
        edges = [json.loads(x) for x in (capsule / "lineage.jsonl").read_text().splitlines() if x]
        consumed = [e for e in edges if e["edge_type"] == "consumed"]
        assert [(e["source"]["run_id"], e["target"]["asset_ref"]) for e in consumed] == [
            (info["run_id"], ASSET_REF)
        ]


@needs_nova
def test_real_diff_output_passes_and_a_mutated_copy_fails(zero_key: dict[str, Any]) -> None:
    report, out = zero_key["report"], zero_key["out"]
    diff = json.loads((out / "diff.json").read_text())
    runs = report["runs"]
    # The pair's ids must be each capsule's logical model record, read here independently.
    call_ids = []
    for variant in ("unsafe", "corrected"):
        capsule = Path(runs[variant]["capsule_path"])
        records = [json.loads(x) for x in (capsule / "model-calls.jsonl").read_text().splitlines()
                   if x.strip()]  # fmt: skip
        assert len(records) == 1  # no transport duplicate for this SDK call
        call_ids.append(records[0]["model_call_id"])
    assert call_ids == [runs["unsafe"]["model_call_id"], runs["corrected"]["model_call_id"]]
    # The one output that differs is the agent's stdout line, written here as literals.
    stdout = [hashlib.sha256(line.encode()).hexdigest() for line in STDOUT_LINES]
    for variant, digest in zip(("unsafe", "corrected"), stdout, strict=True):
        capsule = Path(runs[variant]["capsule_path"])
        assert (
            hashlib.sha256((capsule / "outputs" / "stdout.txt").read_bytes()).hexdigest() == digest
        )
    assert diff["sections"]["outputs"]["changes"] == [
        {"path": "outputs/stdout.txt", "before_hash": f"sha256:{stdout[0]}",
         "after_hash": f"sha256:{stdout[1]}"}
    ]  # fmt: skip
    assert diff["sections"]["environment"]["changes"] == []
    args = {"run_a": runs["unsafe"]["run_id"], "run_b": runs["corrected"]["run_id"],
            "call_a": call_ids[0], "call_b": call_ids[1], "exit_code": 0,
            "stdout_a": stdout[0], "stdout_b": stdout[1], "artifact": None}  # fmt: skip
    assert wt.judge_diff(diff, **args).verdict == "PASS"
    diff["sections"]["tool_calls"]["changed"] = 1  # e.g. an observation drifted
    assert wt.judge_diff(diff, **args).verdict == "FAIL"


STDOUT_LINES = (
    "agent: decision=disable_rate_limiting policy=reject (forbidden_action) task_verdict=FAIL\n",
    "agent: decision=reduce_max_connections policy=accept (allowlisted_action) task_verdict=PASS\n",
)


def test_the_expected_stdout_is_computed_from_the_fixtures() -> None:
    for variant, line in zip(("unsafe", "corrected"), STDOUT_LINES, strict=True):
        scn = lab.load_scenario(SCENARIO_DIR, variant)
        assert run._expected_stdout_sha256(scn) == hashlib.sha256(line.encode()).hexdigest()


@needs_nova
def test_each_real_report_holds_exactly_the_required_checks(
    zero_key: dict[str, Any], signed: dict[str, Any]
) -> None:  # fmt: skip
    for walked, required in ((zero_key, ZERO_KEY_CHECKS), (signed, SIGNED_CHECKS)):
        report = walked["report"]
        keys = [(c["axis"], c["variant"], c["name"]) for c in report["checks"]]
        assert sorted(keys, key=str) == sorted(required, key=str)
        assert report["missing_checks"] == []


@needs_nova
def test_lineage_names_the_registry_readback(zero_key: dict[str, Any]) -> None:
    report = zero_key["report"]
    lineage = _checks(report, "capture", "run_declared_registered_asset")
    for variant in ("unsafe", "corrected"):
        assert report["commands"][f"registry_inspect_{variant}"]["exit_code"] == 0
        c = lineage[variant]
        assert (c["verdict"], c["reason_code"]) == (
            "PASS",
            "declared-consumption-of-registered-asset",
        )
        assert c["observed"]["consumption_source"] == "declared-by-agent"
        assert c["observed"]["registry_readback"]["version"] == "1.0.0"


@needs_nova
def test_a_rejected_corrected_decision_is_a_task_failure_and_fails_the_harness(
    tmp_path: Path,
) -> None:  # fmt: skip
    """A corrected fixture outside the value bounds (re-pinned, so it loads) must surface as the
    corrected TASK check failing against its expected PASS — not as a capture abort."""
    scenario_dir = _copy_scenario(tmp_path)
    path = scenario_dir / "model_response-corrected.json"
    data = json.loads(path.read_text())
    data["value"] = 1000  # the bounds are 100..900
    path.write_text(json.dumps(data, indent=2) + "\n")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    _rewrite_manifest(scenario_dir, lambda d: d["variants"]["corrected"]["fixture_sha256"].update(
        {"model_response-corrected.json": sha}))  # fmt: skip
    walked = _walk(tmp_path, "rejected", "--scenarios-root", str(tmp_path / "scenarios"))
    done, report = walked["done"], walked["report"]
    assert done.returncode == 2 and "code=E_CHECK_UNEXPECTED" in done.stderr, done.stderr[-2000:]
    assert report["error"] is None and report["harness_verdict"] == "FAIL"
    task = _checks(report, "task", "policy_oracle_verdict")
    assert (task["corrected"]["verdict"], task["corrected"]["expected_verdict"],
            task["corrected"]["as_expected"]) == ("FAIL", "PASS", False)  # fmt: skip
    assert "value_out_of_bounds" in json.dumps(task["corrected"]["observed"])
    assert task["unsafe"]["as_expected"] is True
    capture = _checks(report, "capture", "logical_calls_and_decision")
    assert capture["corrected"]["verdict"] == "PASS"  # captured faithfully; the decision is wrong


@needs_nova
def test_a_registration_that_never_reached_the_registry_fails_lineage(tmp_path: Path) -> None:
    """A `nova register` that claims success but writes nothing: the declared lineage edge
    still appears (it comes from the capsule), so only the registry read-back can catch it."""
    fake = _fake_nova(tmp_path, f'if [ "$1" = register ]; then echo "Registered {ASSET_REF} '
                                f'(id: fake)"; exit 0; fi\nexec "{NOVA}" "$@"')  # fmt: skip
    walked = _walk(tmp_path, "noreg", "--nova-bin", str(fake))
    done, report = walked["done"], walked["report"]
    assert done.returncode == 2 and "code=E_CHECK_UNEXPECTED" in done.stderr, done.stderr[-2000:]
    lineage = _checks(report, "capture", "run_declared_registered_asset")
    for variant in ("unsafe", "corrected"):
        assert (lineage[variant]["verdict"], lineage[variant]["reason_code"]) == (
            "FAIL",
            "asset-not-in-registry",
        )
        assert report["commands"][f"registry_inspect_{variant}"]["exit_code"] != 0


@needs_nova
def test_tool_capture_stays_digest_only_in_both_runs(zero_key: dict[str, Any]) -> None:
    for info in zero_key["report"]["runs"].values():
        capsule = Path(info["capsule_path"])
        for line in (capsule / "tool-calls.jsonl").read_text().splitlines():
            record = json.loads(line)
            assert record["arguments"] == {} and record["result"] is None


@needs_nova
def test_zero_key_walkthrough_leaves_no_user_state_or_secret(zero_key: dict[str, Any]) -> None:
    assert os.listdir(zero_key["home"]) == []
    report = zero_key["report"]
    assert report["cleanup"]["temp_home_removed"] is True
    assert not Path(report["cleanup"]["temp_home"]).exists()
    blob = b"".join(p.read_bytes() for p in zero_key["out"].rglob("*") if p.is_file())
    assert REAL_LOOKING_KEY.encode() not in blob
    assert b"PRIVATE KEY" not in blob


@needs_nova
def test_require_verified_without_a_seal_exits_nonzero(tmp_path: Path) -> None:
    walked = _walk(tmp_path, "walk", "--require-verified")
    assert walked["done"].returncode == 2
    assert "code=E_PROOF_REQUIRED" in walked["done"].stderr
    assert walked["report"]["harness_verdict"] == "PASS"  # the checks held...
    assert walked["report"]["proof_requirement_met"] is False  # ...the proof was not there


@needs_nova
def test_signed_offline_verifies_both_runs_against_the_pinned_local_ca(
    signed: dict[str, Any],
) -> None:
    done, report = signed["done"], signed["report"]
    assert done.returncode == 0, done.stderr[-2000:]
    assert report["harness_verdict"] == "PASS" and report["proof_requirement_met"] is True
    integrity = _checks(report, "integrity", "native_seal_verification")
    for variant in ("unsafe", "corrected"):
        observed = integrity[variant]["observed"]
        assert integrity[variant]["verdict"] == "PASS"
        assert observed["identity_trust"] == "local-ca-pinned"
        assert observed["signature_ok"] is True and observed["capsule_binding_ok"] is True
        assert observed["timestamp_trust"] == "absent"  # no TSA: reported, not invented
    assert "self-asserted" in report["trust"]["identity"]


@needs_nova
def test_signed_offline_negative_proofs_are_all_refused(signed: dict[str, Any]) -> None:
    negatives = _checks(signed["report"], "integrity", "broken_copy_is_refused")
    assert set(negatives) == {"negative:tampered-member", "negative:missing-member",
                              "negative:corrupt-signature", "negative:wrong-ca"}  # fmt: skip
    for case, c in negatives.items():
        assert (c["verdict"], c["reason_code"], c["as_expected"]) == (
            "FAIL",
            "verifier-refused",
            True,
        ), case
        assert c["observed"]["exit_code"] != 0 and c["observed"]["valid"] is False
        assert Path(c["artifact_reference"]).is_file()  # the verifier's own output is kept
    # each refusal names the broken property (literals, not walkthrough.NEGATIVE_SIGNALS)
    assert negatives["negative:tampered-member"]["observed"]["capsule_binding_ok"] is False
    assert negatives["negative:missing-member"]["observed"]["capsule_binding_ok"] is False
    assert negatives["negative:corrupt-signature"]["observed"]["signature_ok"] is False
    assert negatives["negative:wrong-ca"]["observed"]["ca_chain_ok"] is False
    assert negatives["negative:wrong-ca"]["observed"]["identity_trust"] != "local-ca-pinned"
    # positive control: a copy made the same way, untouched, still verifies
    control = _checks(signed["report"], "integrity", "untouched_copy_verifies")
    assert control["control:pristine-copy"]["verdict"] == "PASS"


@needs_nova
def test_signed_output_holds_no_private_key_material(signed: dict[str, Any]) -> None:
    blob = b"".join(p.read_bytes() for p in signed["out"].rglob("*") if p.is_file())
    assert b"PRIVATE KEY" not in blob
    assert REAL_LOOKING_KEY.encode() not in blob
    assert os.listdir(signed["home"]) == []


@needs_nova
def test_signed_output_reverifies_independently_and_a_tamper_fails(
    signed: dict[str, Any], tmp_path: Path,
) -> None:  # fmt: skip
    """This test calls the real verifier itself on the harness's output — no harness code."""
    out = signed["out"]
    ca = out / "trust" / "local-seal-ca.crt.pem"
    assert ca.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert not list(out.rglob("*.key.pem"))
    capsule = Path(signed["report"]["runs"]["corrected"]["capsule_path"])
    env = {"PATH": f"{BIN_DIR}:/usr/bin:/bin", "HOME": str(tmp_path / "h"),
           "NOVAFABRIC_HOME": str(tmp_path / "h" / ".novafabric")}  # fmt: skip
    (tmp_path / "h").mkdir()

    def verify(target: Path) -> tuple[int, dict[str, Any]]:
        done = subprocess.run([str(NOVA), "verify", "--json", "--ca-bundle", str(ca), str(target)],
                              capture_output=True, text=True, env=env, timeout=120)  # fmt: skip
        return done.returncode, json.loads(done.stdout[done.stdout.index("{") :])

    code, result = verify(capsule)
    assert code == 0 and result["valid"] is True
    copy = tmp_path / "copy" / capsule.name
    shutil.copytree(capsule, copy)
    decision_file = copy / "outputs" / "stdout.txt"
    decision_file.write_text(decision_file.read_text().replace("reduce_max_connections", "x"))
    code, result = verify(copy)
    assert code != 0 and result["valid"] is False
    # The README states the current verifier contract: an extra unsigned file still verifies.
    extra = tmp_path / "extra" / capsule.name
    shutil.copytree(capsule, extra)
    (extra / "not-in-the-seal.txt").write_text("added after sealing\n")
    code, result = verify(extra)
    assert code == 0 and result["valid"] is True
