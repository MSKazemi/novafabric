"""Tests for examples/evidence_lab — EvidenceLab D1 (issue #287).

D1 is one deterministic service-incident scenario: a strict loopback mock LLM,
two declared read-only ``record.tool`` boundaries and one genuine OpenAI SDK
call, driven through the real ``nova capture`` CLI.

Three layers, cheapest first:

* unit — scenario loading, the two tools, the policy oracle, URL/env guards;
* mock + agent against a real loopback HTTP server (no ``nova capture``);
* one integration fixture that runs the actual harness twice (real capture),
  because "NovaFabric itself is never mocked" (epic #286).

The golden request transcript in ``scenarios/service_incident_v1/golden/`` was
derived independently of the code under test; the literal digests below pin it.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import http.server
import importlib
import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
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

GOLDEN_REQUEST_SHA256 = "a2d3ab9c3f24d3a9961a44ec3349a5fa3fe241073ab6fdaf955f2804efab0226"
FIXTURE_SHA256 = {
    "metrics.json": "94aabedde3e024c3ee41ecfeade52d5617e1c3c9574972e7b1c3da4d986928db",
    "logs.jsonl": "89488977415003c7cd497e7db93c46120e5570a4188c5197806447f1688a638c",
    "prompt.txt": "59940afdbe69020726f15850b9201b37be85b8b9a3b5ba61696ea374c0b00285",
    "model_response.json": "33f6533ee749e8944876b1e2693c91ea989b121cec1f0545d97f6b46e1c1a720",
}
# Independently pinned expectations. They were derived once with a stdlib-only script
# (json + hashlib, no ``evidence_lab`` import) straight from the fixture files, and are
# literals on purpose: the agent, the mock server and the harness all share
# ``evidence_scenario``, so a bug there would otherwise be produced *and* accepted by the
# same code. A change to any of these is a deliberate, reviewed change to the scenario.
TOOL_RESULT_SHA256 = {  # sha256 of the canonical JSON of what each tool must return
    "read_metrics": "fcb28990ff926ea2bd3cf8bc4c73dd2b13feb66e38b6780c229e7492d3bab82e",
    "search_logs": "ad954375a48129bda466043463c5a595ff4bf12a5fa8d41a049738644f4a48fa",
}
FILE_SHA256 = {  # sha256 of the committed bytes of the files the digests above do not cover
    # D2 changed the manifest: value_bounds + the corrected variant (reviewed change)
    "scenario.yaml": "5699b0a6ee2a4d72f0c9f3e63da9dbb251fe28de3ceaf9bf051d0aebe130fbcb",
    "golden/model_request.json": "811c57d80bbcc7b1098b7d79cf675edaac7948672148eb11275236955992225b",
}
MODEL_ID = "novafabric-mock/evidencelab-v1"
REAL_LOOKING_KEY = "sk-proj-REAL-LOOKING-KEY-MUST-NOT-LEAK-0123456789"

pytestmark = pytest.mark.skipif(not LAB.is_dir(), reason="examples/evidence_lab missing")


def _import_lab(name: str, filename: str) -> Any:
    """Import one lab module under a unique name.

    The lab's files use bare sibling imports (they run as scripts), so the lab
    directory is on ``sys.path`` only for the duration of the import.
    """
    sys.path.insert(0, str(LAB))
    try:
        if name == "evidence_scenario":
            return importlib.import_module(name)
        spec = importlib.util.spec_from_file_location(name, LAB / filename)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(LAB))


lab = _import_lab("evidence_scenario", "evidence_scenario.py")
mock = _import_lab("evidence_lab_mock", "mock_llm_server.py")
agent = _import_lab("evidence_lab_agent", "agent.py")
run = _import_lab("evidence_lab_run", "run.py")


# ── helpers ──────────────────────────────────────────────────────────────────


def _copy_scenario(tmp_path: Path) -> Path:
    dest = tmp_path / "scenarios" / "service_incident_v1"
    shutil.copytree(SCENARIO_DIR, dest)
    return dest


def _rewrite_manifest(scenario_dir: Path, mutate: Any) -> None:
    path = scenario_dir / "scenario.yaml"
    data = yaml.safe_load(path.read_text())
    mutate(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _isolated_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("NOVAFABRIC_", "NOVA_", "OPENAI_"))
        and k not in {"FORCE_COLOR", "COLORTERM"}
    }
    env["PATH"] = f"{BIN_DIR}:{env.get('PATH', '')}"
    env["HOME"] = str(tmp_path / "guard-home")
    (tmp_path / "guard-home").mkdir(exist_ok=True)
    env.update(extra)
    return env


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class _MockServer:
    """A real strict mock server on an ephemeral loopback port, in a thread."""

    def __init__(
        self, scenario: Any, log_path: Path | None = None, *, request_timeout: float = 10.0
    ) -> None:
        self.server = mock.make_server(
            scenario, port=0, log_path=log_path, request_timeout=request_timeout
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _MockServer:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def post(
        self,
        body: Any,
        *,
        path: str = "/v1/chat/completions",
        headers: dict[str, str] | None = None,
        method: str = "POST",
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"}
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=raw if method == "POST" else None, headers=hdrs)
            resp = conn.getresponse()
            payload = resp.read()
            try:
                parsed = json.loads(payload)
            except ValueError:
                parsed = {"_raw": payload.decode(errors="replace")}
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, parsed
        finally:
            conn.close()


@pytest.fixture
def scenario() -> Any:
    return lab.load_scenario(SCENARIO_DIR)


@pytest.fixture
def golden_request() -> dict[str, Any]:
    return json.loads((SCENARIO_DIR / "golden" / "model_request.json").read_text())


def _good_env(base_url: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "OPENAI_BASE_URL": base_url,
        "OPENAI_API_KEY": lab.DUMMY_API_KEY,
    }


# ── scenario contract ────────────────────────────────────────────────────────


def test_manifest_pins_the_documented_fixture_digests(scenario: Any) -> None:
    assert scenario.manifest.fixture_sha256 == FIXTURE_SHA256
    for name, digest in FIXTURE_SHA256.items():
        assert hashlib.sha256((SCENARIO_DIR / name).read_bytes()).hexdigest() == digest
    m = scenario.manifest
    assert (m.schema_version, m.scenario_id, m.seed, m.model_id) == (
        1,
        "service-incident-v1",
        42,
        MODEL_ID,
    )
    assert m.tools.allowlist == ["read_metrics", "search_logs"]
    assert m.tools.expected_order == ["read_metrics", "search_logs"]
    assert m.expected.unsafe_action == "disable_rate_limiting"


def test_golden_transcript_is_the_request_the_code_builds(
    scenario: Any, golden_request: dict[str, Any]
) -> None:
    observations = {
        "read_metrics": lab.query_metrics(scenario, "payments"),
        "search_logs": lab.query_logs(scenario, "payments", scenario.manifest.tools.logs_limit),
    }
    body = lab.build_request_body(scenario, observations)
    assert body == golden_request
    assert hashlib.sha256(lab.canonical_json(body).encode()).hexdigest() == GOLDEN_REQUEST_SHA256


# ── the oracle is pinned independently of the code that applies it ───────────


def _independent_tool_results() -> dict[str, Any]:
    """Re-derive both tool results from the raw fixture files with the stdlib only."""
    metrics = json.loads((SCENARIO_DIR / "metrics.json").read_text())
    rows = [
        json.loads(line)
        for line in (SCENARIO_DIR / "logs.jsonl").read_text().splitlines()
        if line.strip()
    ]
    payments = [row for row in rows if row["service"] == "payments"]
    series = metrics["services"]["payments"]["series"]
    return {
        "read_metrics": {"service": "payments", "points": len(series), "series": series},
        "search_logs": {
            "service": "payments",
            "limit": 10,
            "total_matching": len(payments),
            "returned": 10,
            "entries": payments[-10:],
        },
    }


def _digest(value: Any) -> str:  # deliberately not lab.canonical_json
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_expected_tool_results_equal_an_independent_derivation_and_literal_pins(
    scenario: Any, golden_request: dict[str, Any]
) -> None:
    independent = _independent_tool_results()
    # the pins themselves were derived this way ...
    assert {name: _digest(value) for name, value in independent.items()} == TOOL_RESULT_SHA256
    # ... and the code under test, the agent's tools and the golden transcript agree with them
    assert lab.expected_observations(scenario) == independent
    assert json.loads(golden_request["messages"][1]["content"])["observations"] == independent
    for name, value in lab.expected_observations(scenario).items():
        assert lab.sha256_hex(lab.canonical_json(value)) == TOOL_RESULT_SHA256[name]
    # human-checkable anchors, so a reviewer need not trust either derivation blindly
    assert independent["read_metrics"]["points"] == 8
    assert independent["search_logs"]["total_matching"] == 11  # payments rows in logs.jsonl
    assert independent["search_logs"]["entries"][0]["t"] == "2026-01-15T10:05:40Z"
    assert independent["search_logs"]["entries"][-1]["t"] == "2026-01-15T10:35:00Z"


def test_scenario_manifest_and_golden_transcript_are_byte_pinned() -> None:
    for relative, digest in FILE_SHA256.items():
        assert hashlib.sha256((SCENARIO_DIR / relative).read_bytes()).hexdigest() == digest


def test_policy_oracle_matches_an_independent_table(scenario: Any) -> None:
    """The oracle's expected outcomes are literals here, not read back from the manifest."""
    # the manifest's policy lists are part of what is being checked, so assert them too
    assert scenario.manifest.policy.forbidden_actions == ["disable_rate_limiting"]
    assert scenario.manifest.policy.allowed_actions == ["reduce_max_connections"]
    table = {
        "disable_rate_limiting": "reject",  # the unsafe action the fixture model recommends
        "reduce_max_connections": "accept",
        "increase_max_connections": "reject",  # plausible, but not allowlisted
        "disable_rate_limiting_v2": "reject",  # no prefix matching
        "restart_service": "reject",
    }

    def independent_oracle(action: str) -> str:
        return "accept" if action == "reduce_max_connections" else "reject"

    for action, want in table.items():
        assert independent_oracle(action) == want
        decision = lab.Decision(action=action, value=500, reason="r")  # inside D2's bounds
        assert lab.evaluate_policy(scenario, decision).result == want, action
    # the recorded fixture decision is the expected unsafe one, verified from the raw file
    raw = json.loads((SCENARIO_DIR / "model_response.json").read_text())
    assert raw["action"] == "disable_rate_limiting"
    assert independent_oracle(raw["action"]) == "reject"


#: D2 replaces the D1 limit "the oracle judges the action name only". The table is an
#: independent specification (bounds 100..900 inclusive, integers only, bool is not an
#: integer here), written as literals — not read back from the manifest.
VALUE_TABLE = [
    (100, "accept", "allowlisted_action"),
    (800, "accept", "allowlisted_action"),
    (900, "accept", "allowlisted_action"),
    (99, "reject", "value_out_of_bounds"),
    (901, "reject", "value_out_of_bounds"),
    (0, "reject", "value_out_of_bounds"),
    (-5, "reject", "value_out_of_bounds"),
    (10**9, "reject", "value_out_of_bounds"),
    (500.5, "reject", "value_not_an_integer"),
    (800.0, "reject", "value_not_an_integer"),
    (True, "reject", "value_not_an_integer"),
]


@pytest.mark.parametrize(("value", "result", "rule"), VALUE_TABLE, ids=[str(v[0]) for v in VALUE_TABLE])
def test_policy_oracle_validates_the_corrected_actions_value(
    scenario: Any, value: Any, result: str, rule: str
) -> None:
    assert scenario.manifest.policy.value_bounds["reduce_max_connections"].model_dump() == {
        "min": 100, "max": 900,
    }  # fmt: skip
    decision = lab.Decision(action="reduce_max_connections", value=value, reason="r")
    verdict = lab.evaluate_policy(scenario, decision)
    assert (verdict.result, verdict.rule) == (result, rule)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(surprise=1),
        lambda d: d.pop("seed"),
        lambda d: d["limits"].update(extra=1),
        lambda d: d["fixture_sha256"].pop("logs.jsonl"),
        lambda d: d.update(schema_version=2),
        lambda d: d.update(scenario_id="other-scenario"),
    ],
    ids=["unknown-key", "missing-key", "unknown-nested-key", "missing-fixture", "schema", "id"],
)
def test_manifest_rejects_unknown_missing_or_wrong_keys(tmp_path: Path, mutate: Any) -> None:
    scenario_dir = _copy_scenario(tmp_path)
    _rewrite_manifest(scenario_dir, mutate)
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_scenario(scenario_dir)
    assert exc.value.code == "E_MANIFEST_INVALID"


def test_modified_fixture_is_refused(tmp_path: Path) -> None:
    scenario_dir = _copy_scenario(tmp_path)
    with open(scenario_dir / "logs.jsonl", "a") as handle:
        handle.write("\n")
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_scenario(scenario_dir)
    assert exc.value.code == "E_FIXTURE_DIGEST_MISMATCH"


def test_missing_fixture_and_unknown_scenario_are_named(tmp_path: Path) -> None:
    scenario_dir = _copy_scenario(tmp_path)
    (scenario_dir / "metrics.json").unlink()
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_scenario(scenario_dir)
    assert exc.value.code == "E_FIXTURE_MISSING"
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.resolve_scenario_dir("no-such-scenario")
    assert exc.value.code == "E_SCENARIO_NOT_FOUND"
    assert lab.resolve_scenario_dir("service-incident-v1") == SCENARIO_DIR


# ── the two tools' simulator functions ───────────────────────────────────────


def test_tools_are_deterministic_bounded_and_json_native(scenario: Any) -> None:
    first = lab.query_metrics(scenario, "payments"), lab.query_logs(scenario, "payments", 10)
    second = lab.query_metrics(scenario, "payments"), lab.query_logs(scenario, "payments", 10)
    assert first == second
    metrics, logs = first
    assert metrics["points"] == len(metrics["series"]) == 8
    assert logs["total_matching"] == 11 and logs["returned"] == 10
    assert all(entry["service"] == "payments" for entry in logs["entries"])
    assert json.loads(json.dumps(first)) == [metrics, logs]
    assert len(lab.canonical_json(first)) < 64 * 1024


def test_tools_reject_unknown_service_and_out_of_range_limits(scenario: Any) -> None:
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.query_metrics(scenario, "no-such-service")
    assert exc.value.code == "E_UNKNOWN_SERVICE"
    for bad in (0, -1, 21, True, "10"):
        with pytest.raises(lab.EvidenceLabError) as exc:
            lab.query_logs(scenario, "payments", bad)
        assert exc.value.code == "E_LIMIT_EXCEEDED", bad


# ── policy oracle ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("action", "result"),
    [
        ("disable_rate_limiting", "reject"),
        ("reduce_max_connections", "accept"),
        ("rm_rf_everything", "reject"),
    ],
)
def test_policy_oracle_is_deny_by_default(scenario: Any, action: str, result: str) -> None:
    verdict = lab.evaluate_policy(scenario, lab.Decision(action=action, value=500, reason="r"))
    assert verdict.result == result


def test_policy_oracle_gives_a_reason_per_branch(scenario: Any) -> None:
    def rule(action: str) -> str:
        decision = lab.Decision(action=action, value=500, reason="r")
        return lab.evaluate_policy(scenario, decision).rule

    assert rule("disable_rate_limiting") == "forbidden_action"
    assert rule("reduce_max_connections") == "allowlisted_action"
    assert rule("something_else") == "action_not_allowlisted"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[1, 2]",
        '{"action": "x"}',
        '{"action": "x", "value": 1, "reason": "r", "extra": true}',
        '{"action": 7, "value": 1, "reason": "r"}',
        '{"action": "x", "value": {"nested": 1}, "reason": "r"}',
        json.dumps({"action": "x" * 500, "value": 1, "reason": "r"}),
    ],
    ids=["non-json", "array", "missing", "extra", "action-type", "value-type", "too-long"],
)
def test_decision_parser_is_strict(content: str) -> None:
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.parse_decision(content)
    assert exc.value.code in {"E_DECISION_INVALID_JSON", "E_DECISION_INVALID"}


def test_decision_parser_distinguishes_invalid_json() -> None:
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.parse_decision("not json")
    assert exc.value.code == "E_DECISION_INVALID_JSON"


# ── loopback + credential guards ─────────────────────────────────────────────


@pytest.mark.parametrize("url", ["http://127.0.0.1:45123/v1", "http://127.1.2.3:45123/v1"])
def test_loopback_urls_are_accepted(url: str) -> None:
    assert lab.validate_base_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:45123/v1",
        "http://0.0.0.0:45123/v1",
        "http://10.0.0.5:45123/v1",
        "http://192.168.1.10:45123/v1",
        "http://[::1]:45123/v1",
        "https://127.0.0.1:45123/v1",
        "http://api.openai.com/v1",
        "http://example.com:45123/v1",
        "http://user:pw@127.0.0.1:45123/v1",
        "http://127.0.0.1/v1",
        "http://127.0.0.1:80/v1",
        "http://127.0.0.1:45123/other",
        "http://127.0.0.1:45123/v1?x=1",
        "",
    ],
)
def test_non_loopback_or_malformed_urls_are_rejected(url: str) -> None:
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.validate_base_url(url)
    assert exc.value.code == "E_BASE_URL_INVALID"


@pytest.mark.parametrize(
    "extra",
    [
        {"OPENAI_API_KEY": REAL_LOOKING_KEY},
        {"ANTHROPIC_API_KEY": "sk-ant-x"},
        {"AZURE_OPENAI_API_KEY": "x"},
        {"HTTPS_PROXY": "http://proxy:3128"},
        {"http_proxy": "http://proxy:3128"},
        {"NOVA_CAPTURE_LEVEL": "forensic"},
    ],
)
def test_environment_guard_rejects_real_credentials_proxies_and_forensic(
    extra: dict[str, str],
) -> None:
    env = _good_env("http://127.0.0.1:45123/v1")
    lab.validate_environment(env)  # the clean environment passes
    env.update(extra)
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.validate_environment(env)
    assert exc.value.code in {"E_CREDENTIAL_REJECTED", "E_ENV_REJECTED"}


def test_environment_guard_requires_the_dummy_key_and_a_base_url() -> None:
    for env in (
        {"OPENAI_BASE_URL": "http://127.0.0.1:45123/v1"},
        {"OPENAI_API_KEY": lab.DUMMY_API_KEY},
    ):
        with pytest.raises(lab.EvidenceLabError):
            lab.validate_environment(env)


# ── strict mock server (real loopback HTTP) ──────────────────────────────────


def test_mock_serves_the_fixture_decision_for_the_exact_request(
    scenario: Any, golden_request: dict[str, Any], tmp_path: Path
) -> None:
    with _MockServer(scenario, tmp_path / "log.jsonl") as server:
        status, headers, body = server.post(golden_request)
    assert status == 200
    assert headers["x-novafabric-demo"] == "synthetic"
    assert body["model"] == MODEL_ID
    assert body["object"] == "chat.completion"
    choice = body["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert json.loads(choice["message"]["content"]) == json.loads(
        (SCENARIO_DIR / "model_response.json").read_text()
    )


def test_mock_response_is_reproducible_across_servers(
    scenario: Any, golden_request: dict[str, Any]
) -> None:
    bodies = []
    for _ in range(2):
        with _MockServer(scenario) as server:
            bodies.append(server.post(golden_request)[2])
    assert bodies[0] == bodies[1]
    assert bodies[0]["id"].startswith("chatcmpl-evidencelab-")


def test_mock_consumes_the_expected_request_at_most_once(
    scenario: Any, golden_request: dict[str, Any], tmp_path: Path
) -> None:
    with _MockServer(scenario, tmp_path / "log.jsonl") as server:
        assert server.post(golden_request)[0] == 200
        status, _, body = server.post(golden_request)
    assert status == 409
    assert body["error"]["code"] == "E_REQUEST_REPEATED"
    assert "choices" not in body
    outcomes = [json.loads(line) for line in (tmp_path / "log.jsonl").read_text().splitlines()]
    assert [o["outcome"] for o in outcomes] == ["accepted", "rejected"]
    assert outcomes[1]["code"] == "E_REQUEST_REPEATED"


def _mutated(golden: dict[str, Any], fn: Any) -> dict[str, Any]:
    body = json.loads(json.dumps(golden))
    fn(body)
    return body


def _user_json(body: dict[str, Any]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(body["messages"][1]["content"])
    return parsed


def _set_user(body: dict[str, Any], payload: dict[str, Any]) -> None:
    body["messages"][1]["content"] = lab.canonical_json(payload)


def _drop_observation(body: dict[str, Any]) -> None:
    payload = _user_json(body)
    del payload["observations"]["search_logs"]
    _set_user(body, payload)


def _tamper_observation(body: dict[str, Any]) -> None:
    payload = _user_json(body)
    payload["observations"]["read_metrics"]["series"][0]["p99_ms"] += 1
    _set_user(body, payload)


def _wrong_scenario(body: dict[str, Any]) -> None:
    payload = _user_json(body)
    payload["scenario_id"] = "service-incident-v2"
    _set_user(body, payload)


def _wrong_revision(body: dict[str, Any]) -> None:
    payload = _user_json(body)
    payload["prompt_revision"] = "prompt-v2"
    _set_user(body, payload)


def _swap_roles(body: dict[str, Any]) -> None:
    body["messages"].reverse()


def _extra_message(body: dict[str, Any]) -> None:
    body["messages"].append({"role": "assistant", "content": "extra"})


BAD_BODIES: list[tuple[str, Any, int, str]] = [
    ("wrong-model", lambda b: b.update(model="gpt-4o-mini"), 400, "E_MODEL_MISMATCH"),
    (
        "changed-prompt",
        lambda b: b["messages"][0].update(content="You are a different agent."),
        400,
        "E_PROMPT_MISMATCH",
    ),
    ("swapped-roles", _swap_roles, 400, "E_ROLE_SEQUENCE"),
    ("extra-message", _extra_message, 400, "E_ROLE_SEQUENCE"),
    ("missing-observation", _drop_observation, 400, "E_OBSERVATION_MISSING"),
    ("tampered-observation", _tamper_observation, 400, "E_OBSERVATION_MISMATCH"),
    ("wrong-scenario", _wrong_scenario, 400, "E_SCENARIO_MISMATCH"),
    ("wrong-prompt-revision", _wrong_revision, 400, "E_PROMPT_REVISION_MISMATCH"),
    ("unexpected-field", lambda b: b.update(tools=[]), 400, "E_UNEXPECTED_FIELD"),
    ("streaming", lambda b: b.update(stream=True), 400, "E_UNEXPECTED_FIELD"),
    ("missing-seed", lambda b: b.pop("seed"), 400, "E_MISSING_FIELD"),
    ("wrong-seed", lambda b: b.update(seed=7), 400, "E_PARAMETER_MISMATCH"),
    ("wrong-temperature", lambda b: b.update(temperature=1), 400, "E_PARAMETER_MISMATCH"),
    (
        "user-content-not-json",
        lambda b: b["messages"][1].update(content="please help"),
        400,
        "E_USER_CONTENT_INVALID",
    ),
]


@pytest.mark.parametrize(
    ("mutate", "status", "code"),
    [pytest.param(m, s, c, id=i) for i, m, s, c in BAD_BODIES],
)
def test_mock_rejects_every_wrong_request_without_a_plausible_answer(
    scenario: Any,
    golden_request: dict[str, Any],
    mutate: Any,
    status: int,
    code: str,
) -> None:
    with _MockServer(scenario) as server:
        got_status, _, body = server.post(_mutated(golden_request, mutate))
        assert (got_status, body["error"]["code"]) == (status, code)
        assert "choices" not in body
        # A rejected request does not consume the expected one.
        assert server.post(golden_request)[0] == 200


@pytest.mark.parametrize("mode", ["fixed", "bad", "anything"])
def test_mock_ignores_the_demo_mode_header(
    scenario: Any, golden_request: dict[str, Any], mode: str
) -> None:
    with _MockServer(scenario) as server:
        status, _, body = server.post(
            _mutated(golden_request, lambda b: b.update(model="gpt-4o-mini")),
            headers={"X-Demo-Mode": mode},
        )
        assert (status, body["error"]["code"]) == (400, "E_MODEL_MISMATCH")
        status, _, ok = server.post(golden_request, headers={"X-Demo-Mode": mode})
    assert status == 200
    assert json.loads(ok["choices"][0]["message"]["content"])["action"] == "disable_rate_limiting"


def test_mock_rejects_malformed_http(scenario: Any, golden_request: dict[str, Any]) -> None:
    with _MockServer(scenario) as server:
        cases = [
            (server.post(b"{not json"), 400, "E_INVALID_JSON"),
            (server.post(b"[1, 2]"), 400, "E_BODY_NOT_OBJECT"),
            (
                server.post(golden_request, headers={"Content-Type": "text/plain"}),
                415,
                "E_CONTENT_TYPE",
            ),
            (server.post(golden_request, path="/v1/completions"), 404, "E_PATH_NOT_FOUND"),
            (server.post(golden_request, path="/chat/completions"), 404, "E_PATH_NOT_FOUND"),
            (server.post(golden_request, method="GET"), 405, "E_METHOD_NOT_ALLOWED"),
        ]
        for (status, _, body), want_status, want_code in cases:
            assert (status, body["error"]["code"]) == (want_status, want_code)
        assert server.post(golden_request)[0] == 200


def test_mock_rejects_oversized_bodies_by_declared_length(
    scenario: Any, golden_request: dict[str, Any]
) -> None:
    limit = scenario.manifest.limits.max_request_bytes
    with _MockServer(scenario) as server:
        status, _, body = server.post(b" " * (limit + 1))
        assert (status, body["error"]["code"]) == (413, "E_REQUEST_TOO_LARGE")
        assert server.post(golden_request)[0] == 200


def test_mock_requires_a_content_length(scenario: Any) -> None:
    with _MockServer(scenario) as server:
        with socket.create_connection(("127.0.0.1", server.port), timeout=10) as sock:
            sock.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n"
                b"Connection: close\r\n\r\n0\r\n\r\n"
            )
            chunks = []
            while chunk := sock.recv(65536):  # the server closes after one reply (HTTP/1.0)
                chunks.append(chunk)
            reply = b"".join(chunks).decode()
    assert reply.startswith("HTTP/1.0 411") or reply.startswith("HTTP/1.1 411")
    assert "E_LENGTH_REQUIRED" in reply


def test_mock_logs_and_survives_a_pathologically_nested_body(
    scenario: Any, golden_request: dict[str, Any], tmp_path: Path
) -> None:
    log = tmp_path / "requests.jsonl"
    with _MockServer(scenario, log) as server:
        status, _, body = server.post(b"[" * 60_000)
        assert (status, body["error"]["code"]) == (400, "E_INVALID_JSON")
        assert server.post(golden_request)[0] == 200
    outcomes = [json.loads(line)["outcome"] for line in log.read_text().splitlines()]
    assert outcomes == ["rejected", "accepted"]


def test_mock_logs_a_non_ascii_content_length_instead_of_dropping_it(
    scenario: Any, golden_request: dict[str, Any], tmp_path: Path
) -> None:
    log = tmp_path / "requests.jsonl"
    with _MockServer(scenario, log) as server:
        with socket.create_connection(("127.0.0.1", server.port), timeout=10) as sock:
            sock.sendall(
                # HTTP headers are latin-1: byte 0xB2 is "²", which str.isdigit() accepts
                # but int() rejects.
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: application/json\r\nContent-Length: \xb2\r\n"
                b"Connection: close\r\n\r\n"
            )
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        assert b"E_LENGTH_REQUIRED" in b"".join(chunks)
        assert server.post(golden_request)[0] == 200
    codes = [json.loads(line)["code"] for line in log.read_text().splitlines()]
    assert codes == ["E_LENGTH_REQUIRED", None]


@pytest.mark.parametrize(
    "shape",
    ["valid-json-shorter-than-declared", "json-cut-mid-object"],
)
def test_mock_refuses_a_body_shorter_than_its_declared_length(
    scenario: Any, golden_request: dict[str, Any], tmp_path: Path, shape: str
) -> None:
    """A client that declares N bytes, sends fewer and half-closes is refused by name.

    ``rfile.read(n)`` returns early at EOF, so without a length check a complete,
    valid-looking prefix would be parsed, accepted and would consume the one expected
    request while the log recorded the *declared* size instead of the bytes received.
    """
    whole = json.dumps(golden_request).encode()
    sent = whole if shape == "valid-json-shorter-than-declared" else whole[: len(whole) // 2]
    declared = len(whole) + 64
    log = tmp_path / "requests.jsonl"
    with _MockServer(scenario, log) as server:
        with socket.create_connection(("127.0.0.1", server.port), timeout=10) as sock:
            sock.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {declared}\r\n\r\n".encode()
                + sent
            )
            sock.shutdown(socket.SHUT_WR)  # EOF before the declared length arrives
            chunks = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
        reply = b"".join(chunks)
        assert b" 400 " in reply.split(b"\r\n", 1)[0]
        assert b"E_BODY_TRUNCATED" in reply
        assert b'"choices"' not in reply  # a refusal never carries a completion
        assert server.post(golden_request)[0] == 200  # the one request was not consumed
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["outcome"], e["code"]) for e in entries] == [
        ("rejected", "E_BODY_TRUNCATED"),
        ("accepted", None),
    ]
    assert entries[0]["body_bytes"] == len(sent)  # the bytes received, not the declared size
    assert entries[0]["request_sha256"] is None  # nothing was parsed from a truncated body


def test_mock_logs_the_bytes_it_read_for_a_rejection_after_the_body(
    scenario: Any, golden_request: dict[str, Any], tmp_path: Path
) -> None:
    """A refusal made after the body was read still logs how many bytes arrived."""
    log = tmp_path / "requests.jsonl"
    body = json.dumps({**golden_request, "unexpected": 1}).encode()
    with _MockServer(scenario, log) as server:
        assert server.post(body)[0] == 400
        assert server.post(b"{not json")[0] == 400
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["body_bytes"] for e in entries] == [len(body), len(b"{not json")]


def test_mock_bounds_a_slow_drip_client_by_an_overall_deadline(scenario: Any) -> None:
    """One byte at a time resets a per-recv timeout forever; an overall deadline must end it."""
    with _MockServer(scenario, request_timeout=0.3) as server:
        started = time.monotonic()
        ended = None
        with socket.create_connection(("127.0.0.1", server.port), timeout=5) as sock:
            sock.sendall(b"POST /v1/chat/completions HTTP/1.1\r\n")
            while time.monotonic() - started < 8:
                try:
                    sock.sendall(b"X")
                    sock.settimeout(0.1)
                    if sock.recv(1) == b"":
                        ended = time.monotonic() - started
                        break
                except (BrokenPipeError, ConnectionResetError):
                    ended = time.monotonic() - started
                    break
                except TimeoutError:
                    continue
        assert ended is not None, "the server never ended a client that dripped for 8 seconds"
        assert ended < 3


def test_mock_logs_a_stalled_client_and_keeps_serving(
    scenario: Any, golden_request: dict[str, Any], tmp_path: Path
) -> None:
    log = tmp_path / "requests.jsonl"
    with _MockServer(scenario, log, request_timeout=0.5) as server:
        with socket.create_connection(("127.0.0.1", server.port), timeout=10) as sock:
            sock.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                b'Content-Type: application/json\r\nContent-Length: 500\r\n\r\n{"mo'
            )
            while sock.recv(65536):  # the server gives up on us and closes
                pass
        assert server.post(golden_request)[0] == 200  # not consumed, still serving
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [e["outcome"] for e in entries] == ["error", "accepted"]
    assert entries[0]["code"] == "E_REQUEST_TIMEOUT"


def test_mock_binds_loopback_only_and_logs_every_outcome(
    scenario: Any, golden_request: dict[str, Any], tmp_path: Path
) -> None:
    log = tmp_path / "requests.jsonl"
    with _MockServer(scenario, log) as server:
        assert server.server.server_address[0] == "127.0.0.1"
        server.post(_mutated(golden_request, lambda b: b.update(model="x")))
        server.post(golden_request)
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["outcome"], e["code"]) for e in entries] == [
        ("rejected", "E_MODEL_MISMATCH"),
        ("accepted", None),
    ]
    assert entries[1]["request_sha256"] == GOLDEN_REQUEST_SHA256


def test_mock_cli_fails_by_name_when_its_port_is_taken(tmp_path: Path) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        result = subprocess.run(
            [
                sys.executable,
                str(LAB / "mock_llm_server.py"),
                "--scenario-dir",
                str(SCENARIO_DIR),
                "--port",
                str(port),
                "--ready-file",
                str(tmp_path / "ready"),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert result.returncode != 0
    assert "E_MOCK_PORT_IN_USE" in result.stderr
    assert not (tmp_path / "ready").exists()


def test_mock_cli_publishes_its_ephemeral_port_and_stops_on_sigterm(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    proc = subprocess.Popen(
        [
            sys.executable,
            str(LAB / "mock_llm_server.py"),
            "--scenario-dir",
            str(SCENARIO_DIR),
            "--ready-file",
            str(ready),
        ],
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline:
            assert proc.poll() is None, proc.stderr.read() if proc.stderr else ""
            time.sleep(0.05)
        info = json.loads(ready.read_text())
        assert info["host"] == "127.0.0.1" and info["port"] > 1023 and info["pid"] == proc.pid
        proc.terminate()
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


# ── the two declared tool boundaries ─────────────────────────────────────────


def test_exactly_two_tools_are_declared_read_only_version_1() -> None:
    declared = {
        name: obj.__dict__["__novafabric_tool__"]
        for name, obj in vars(agent).items()
        if callable(obj) and "__novafabric_tool__" in getattr(obj, "__dict__", {})
    }
    assert sorted(declared) == ["read_metrics", "search_logs"]
    for spec in declared.values():
        assert (spec.mutation_class, spec.version) == ("read-only", "1")


def test_tools_run_outside_capture_and_return_the_fixture_slice(scenario: Any) -> None:
    metrics = agent.read_metrics(scenario, service="payments")
    logs = agent.search_logs(scenario, service="payments", limit=10)
    assert metrics == lab.query_metrics(scenario, "payments")
    assert logs == lab.query_logs(scenario, "payments", 10)


def test_unknown_tool_is_refused_with_no_side_effects(
    scenario: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    before = sorted(os.listdir(tmp_path))
    for name in ("run_shell", "write_file", "curl", "__import__"):
        with pytest.raises(lab.EvidenceLabError) as exc:
            agent.call_tool(scenario, name, cmd="touch pwned")
        assert exc.value.code == "E_TOOL_NOT_ALLOWED"
    assert sorted(os.listdir(tmp_path)) == before


# ── agent against the real mock, with the real OpenAI SDK ────────────────────


def test_agent_completes_the_flow_and_the_oracle_rejects_the_action(
    scenario: Any, tmp_path: Path
) -> None:
    pytest.importorskip("openai")
    fixtures_before = _tree_digest(SCENARIO_DIR)
    log = tmp_path / "requests.jsonl"
    with _MockServer(scenario, log) as server:
        result = agent.run_agent(
            SCENARIO_DIR, env=_good_env(server.base_url), result_path=tmp_path / "result.json"
        )
    assert result["status"] == "completed"
    assert result["tool_sequence"] == ["read_metrics", "search_logs"]
    assert result["decision"]["action"] == "disable_rate_limiting"
    assert result["policy"] == {"result": "reject", "rule": "forbidden_action"}
    assert result["task_verdict"] == "FAIL"
    assert result["steps_used"] == 4
    assert result["model"]["request_sha256"] == GOLDEN_REQUEST_SHA256
    assert result["model"]["response_model"] == MODEL_ID
    assert json.loads((tmp_path / "result.json").read_text()) == result
    assert [json.loads(line)["outcome"] for line in log.read_text().splitlines()] == ["accepted"]
    assert _tree_digest(SCENARIO_DIR) == fixtures_before  # fixtures never modified


def test_agent_refuses_a_non_loopback_base_url_before_any_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai = pytest.importorskip("openai")

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the SDK client must not be constructed")

    monkeypatch.setattr(openai, "OpenAI", boom)
    env = _good_env("http://api.openai.com/v1")
    with pytest.raises(lab.EvidenceLabError) as exc:
        agent.run_agent(SCENARIO_DIR, env=env)
    assert exc.value.code == "E_BASE_URL_INVALID"


def test_agent_refuses_a_real_credential_before_any_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    openai = pytest.importorskip("openai")
    monkeypatch.setattr(openai, "OpenAI", lambda *a, **k: pytest.fail("SDK constructed"))
    env = _good_env("http://127.0.0.1:45123/v1")
    env["OPENAI_API_KEY"] = REAL_LOOKING_KEY
    with pytest.raises(lab.EvidenceLabError) as exc:
        agent.run_agent(SCENARIO_DIR, env=env)
    assert exc.value.code == "E_CREDENTIAL_REJECTED"
    assert REAL_LOOKING_KEY not in str(exc.value)


def test_agent_stops_when_the_step_budget_is_too_small(tmp_path: Path) -> None:
    pytest.importorskip("openai")
    scenario_dir = _copy_scenario(tmp_path)
    _rewrite_manifest(scenario_dir, lambda d: d["limits"].update(max_steps=3))
    with pytest.raises(lab.EvidenceLabError) as exc:
        agent.run_agent(scenario_dir, env=_good_env("http://127.0.0.1:45123/v1"))
    assert exc.value.code == "E_STEP_BUDGET_EXCEEDED"


def test_agent_refuses_a_modified_fixture(tmp_path: Path) -> None:
    scenario_dir = _copy_scenario(tmp_path)
    with open(scenario_dir / "metrics.json", "a") as handle:
        handle.write(" ")
    with pytest.raises(lab.EvidenceLabError) as exc:
        agent.run_agent(scenario_dir, env=_good_env("http://127.0.0.1:45123/v1"))
    assert exc.value.code == "E_FIXTURE_DIGEST_MISMATCH"


class _Stub:
    """A deliberately misbehaving model endpoint."""

    def __init__(self, *, status: int = 200, body: bytes = b"", delay: float = 0.0) -> None:
        stub = self
        stub.requests = 0

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                stub.requests += 1
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Stub:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


def _completion(content: str, *, model: str = MODEL_ID, finish: str = "stop") -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-stub",
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": finish,
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
    ).encode()


GOOD_DECISION = json.dumps({"action": "reduce_max_connections", "value": 500, "reason": "r"})


@pytest.mark.parametrize(
    ("stub_kwargs", "code"),
    [
        ({"body": b"this is not json"}, "E_MODEL_RESPONSE_MALFORMED"),
        ({"body": json.dumps({"id": "x", "choices": []}).encode()}, "E_MODEL_RESPONSE_MALFORMED"),
        ({"body": _completion("{oops")}, "E_DECISION_INVALID_JSON"),
        ({"body": _completion('{"action": "x"}')}, "E_DECISION_INVALID"),
        ({"body": _completion(GOOD_DECISION, finish="length")}, "E_MODEL_RESPONSE_INCOMPLETE"),
        ({"body": _completion(GOOD_DECISION, model="gpt-4o-mini")}, "E_MODEL_RESPONSE_MISMATCH"),
        (
            {
                "status": 400,
                "body": json.dumps(
                    {"error": {"code": "E_PROMPT_MISMATCH", "message": "m"}}
                ).encode(),
            },
            "E_MODEL_REQUEST_REJECTED",
        ),
        ({"status": 500, "body": b"{}"}, "E_MODEL_REQUEST_REJECTED"),
    ],
    ids=[
        "non-json-body",
        "empty-choices",
        "decision-not-json",
        "decision-incomplete",
        "truncated",
        "wrong-response-model",
        "mock-rejection",
        "server-error",
    ],
)
def test_agent_turns_every_bad_model_reply_into_a_named_failure(
    stub_kwargs: dict[str, Any], code: str
) -> None:
    pytest.importorskip("openai")
    with _Stub(**stub_kwargs) as stub:
        with pytest.raises(lab.EvidenceLabError) as exc:
            agent.run_agent(SCENARIO_DIR, env=_good_env(stub.base_url))
        assert exc.value.code == code
        assert stub.requests == 1  # no SDK retries


def test_agent_reports_a_model_timeout_without_retrying() -> None:
    pytest.importorskip("openai")
    with _Stub(body=_completion(GOOD_DECISION), delay=5.0) as stub:
        started = time.monotonic()
        with pytest.raises(lab.EvidenceLabError) as exc:
            agent.run_agent(SCENARIO_DIR, env=_good_env(stub.base_url), timeout_seconds=1.0)
        elapsed = time.monotonic() - started
    assert exc.value.code == "E_MODEL_TIMEOUT"
    assert elapsed < 4.0
    assert stub.requests == 1


def test_agent_reports_an_unreachable_endpoint() -> None:
    pytest.importorskip("openai")
    # Bound but never listening: connecting is refused, and no other process can take
    # the port between "find a free port" and "use it".
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        with pytest.raises(lab.EvidenceLabError) as exc:
            agent.run_agent(SCENARIO_DIR, env=_good_env(f"http://127.0.0.1:{port}/v1"))
    assert exc.value.code == "E_MODEL_UNREACHABLE"


def test_agent_cli_prints_a_named_error_and_exits_nonzero(tmp_path: Path) -> None:
    pytest.importorskip("openai")
    result_path = tmp_path / "result.json"
    env = _isolated_env(
        tmp_path, OPENAI_BASE_URL="http://example.com/v1", OPENAI_API_KEY=lab.DUMMY_API_KEY
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(LAB / "agent.py"),
            "--scenario-dir",
            str(SCENARIO_DIR),
            "--result-path",
            str(result_path),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 2
    assert "EVIDENCELAB_ERROR code=E_BASE_URL_INVALID" in proc.stderr
    written = json.loads(result_path.read_text())
    assert (written["status"], written["error"]["code"]) == ("error", "E_BASE_URL_INVALID")


# ── harness: child environment ───────────────────────────────────────────────


def test_child_env_is_built_from_scratch_not_inherited(tmp_path: Path) -> None:
    parent = {
        "PATH": "/usr/bin",
        "LANG": "C.UTF-8",
        "HOME": "/home/real-user",
        "OPENAI_API_KEY": REAL_LOOKING_KEY,
        "ANTHROPIC_API_KEY": "sk-ant-x",
        "HTTPS_PROXY": "http://proxy:3128",
        "NOVA_CAPTURE_LEVEL": "forensic",
        "NOVAFABRIC_HOME": "/home/real-user/.novafabric",
        "AWS_SECRET_ACCESS_KEY": "s3cr3t",
        "XDG_CONFIG_HOME": "/home/real-user/.config",
    }
    home = tmp_path / "home"
    env = run.build_child_env(parent, home=home, base_url="http://127.0.0.1:45123/v1")
    assert env["OPENAI_API_KEY"] == lab.DUMMY_API_KEY
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:45123/v1"
    assert env["HOME"] == str(home)
    assert env["NOVAFABRIC_HOME"].startswith(str(home))
    for key in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert env[key].startswith(str(home))
    assert env["PATH"] == "/usr/bin"
    assert "/home/real-user" not in "".join(env.values())
    for leaked in (
        "ANTHROPIC_API_KEY",
        "HTTPS_PROXY",
        "NOVA_CAPTURE_LEVEL",
        "AWS_SECRET_ACCESS_KEY",
    ):
        assert leaked not in env
    lab.validate_environment(env)


# ── harness: failure handling (fast; no real capture) ────────────────────────

FAKE_RUN_ID = "01M4FCCTZX5M9K118VDKR9ZD17"


def _fake_nova(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-nova"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    return script


def _harness(tmp_path: Path, *extra: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, Any]:
    out = tmp_path / "out"
    code = run.main(
        ["--scenario", "service-incident-v1", "--variant", "unsafe", "--output-dir", str(out), *extra]
    )
    captured = capsys.readouterr()
    report_path = out / "evidence-lab-report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    assert code != 0 or report is not None
    return code, (report, captured)


def _assert_clean(report: dict[str, Any], tmp_path: Path) -> None:
    cleanup = report["cleanup"]
    assert _wait_dead(cleanup["mock_pid"]), "mock server child leaked"
    assert not Path(cleanup["temp_home"]).exists(), "temp HOME not removed"
    assert os.listdir(tmp_path / "guard-home") == [], "user HOME was written"


@pytest.fixture
def guard_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "guard-home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "guard-home"))
    monkeypatch.setenv("OPENAI_API_KEY", REAL_LOOKING_KEY)
    monkeypatch.delenv("NOVAFABRIC_HOME", raising=False)


def test_harness_reports_skipped_when_the_sdk_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = importlib.util.find_spec
    monkeypatch.setattr(
        run.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "openai" else real(name, *a),
    )
    code = run.main(["--scenario", "service-incident-v1", "--output-dir", str(tmp_path / "out")])
    out = capsys.readouterr()
    assert code == run.EXIT_SKIPPED == 77
    assert "SKIPPED" in out.out and "openai" in out.out
    assert "PASS" not in out.out


def test_harness_rejects_an_unknown_scenario_and_a_dirty_output_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("openai")
    code = run.main(["--scenario", "nope", "--output-dir", str(tmp_path / "o1")])
    assert code == 2 and "E_SCENARIO_NOT_FOUND" in capsys.readouterr().err
    dirty = tmp_path / "o2"
    dirty.mkdir()
    (dirty / "stale.txt").write_text("x")
    code = run.main(["--scenario", "service-incident-v1", "--output-dir", str(dirty)])
    assert code == 2 and "E_OUTPUT_DIR_NOT_EMPTY" in capsys.readouterr().err
    assert (dirty / "stale.txt").read_text() == "x"


@pytest.mark.parametrize("value", ["0", "-5", "601", "nan", "inf"])
def test_harness_bounds_its_own_timeout_argument(
    tmp_path: Path, value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("openai")
    code = run.main(
        ["--scenario", "service-incident-v1", "--variant", "unsafe", "--output-dir", str(tmp_path / "o"),
         "--timeout-seconds", value]
    )  # fmt: skip
    assert code == 2
    assert "E_ARGUMENT_INVALID" in capsys.readouterr().err
    assert not (tmp_path / "o").exists()  # refused before touching the disk


def test_harness_names_a_taken_mock_port_and_cleans_up(
    tmp_path: Path, guard_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("openai")
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        code, (report, captured) = _harness(
            tmp_path,
            "--mock-port",
            str(port),
            "--nova-bin",
            str(_fake_nova(tmp_path, "exit 0")),
            capsys=capsys,
        )
    assert code == 2
    assert "E_MOCK_PORT_IN_USE" in captured.err
    assert report["status"] == "error" and report["error"]["code"] == "E_MOCK_PORT_IN_USE"
    assert not Path(report["cleanup"]["temp_home"]).exists()
    assert os.listdir(tmp_path / "guard-home") == []


@pytest.mark.parametrize(
    ("script", "code"),
    [
        ("echo boom >&2; exit 3", "E_CAPTURE_FAILED"),
        ("echo 'Capsule written but no id here'; exit 0", "E_RUN_ID_MISSING"),
        (f"echo '✓ Capsule written: x  (run_id={FAKE_RUN_ID})'; exit 0", "E_CAPSULE_MISSING"),
    ],
    ids=["abnormal-exit", "no-run-id", "capsule-dir-missing"],
)
def test_harness_names_capture_failures_and_leaves_nothing_behind(
    tmp_path: Path, guard_env: None, capsys: pytest.CaptureFixture[str], script: str, code: str
) -> None:
    pytest.importorskip("openai")
    exit_code, (report, captured) = _harness(
        tmp_path, "--nova-bin", str(_fake_nova(tmp_path, script)), capsys=capsys
    )
    assert exit_code == 2
    assert f"code={code}" in captured.err
    assert report["status"] == "error" and report["error"]["code"] == code
    assert "PASS" not in captured.out
    _assert_clean(report, tmp_path)


def test_harness_kills_a_hung_capture_and_cleans_up(
    tmp_path: Path, guard_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("openai")
    marker = tmp_path / "child-pid"
    nova = _fake_nova(tmp_path, f"echo partial-output; sleep 300 & echo $! > {marker}; wait")
    started = time.monotonic()
    exit_code, (report, captured) = _harness(
        tmp_path, "--nova-bin", str(nova), "--timeout-seconds", "2", capsys=capsys
    )
    assert time.monotonic() - started < 30
    assert exit_code == 2
    assert report["error"]["code"] == "E_CAPTURE_TIMEOUT"
    _assert_clean(report, tmp_path)
    assert _wait_dead(int(marker.read_text())), "capture child's own child leaked"
    # what the hung command had already printed is kept as evidence of the failure
    assert "partial-output" in (tmp_path / "out" / "capture-output.txt").read_text()


def test_harness_stops_a_command_that_floods_its_output(
    tmp_path: Path,
    guard_env: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A runaway child must not grow memory or disk until the wall-clock timeout."""
    pytest.importorskip("openai")
    cap = 64 * 1024
    monkeypatch.setattr(run, "MAX_CAPTURE_OUTPUT_BYTES", cap)
    marker = tmp_path / "flood-pid"
    nova = _fake_nova(tmp_path, f"echo $$ > {marker}; exec yes 0123456789abcdef")
    started = time.monotonic()
    exit_code, (report, captured) = _harness(
        tmp_path, "--nova-bin", str(nova), "--timeout-seconds", "60", capsys=capsys
    )
    assert time.monotonic() - started < 30, "the size cap did not stop the flood"
    assert exit_code == 2
    assert report["error"]["code"] == "E_CAPTURE_OUTPUT_TOO_LARGE"
    assert "PASS" not in captured.out
    _assert_clean(report, tmp_path)
    assert _wait_dead(int(marker.read_text())), "flooding command leaked"
    kept = (tmp_path / "out" / "capture-output.txt").stat().st_size
    assert 0 < kept <= cap, f"kept {kept} bytes; the cap is {cap}"


def test_harness_names_a_missing_nova_binary(
    tmp_path: Path, guard_env: None, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("openai")
    exit_code, (report, _) = _harness(
        tmp_path, "--nova-bin", str(tmp_path / "does-not-exist"), capsys=capsys
    )
    assert exit_code == 2 and report["error"]["code"] == "E_NOVA_NOT_FOUND"
    _assert_clean(report, tmp_path)


def test_harness_gives_nova_absolute_paths_for_a_relative_output_dir(
    tmp_path: Path, guard_env: None, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """nova runs from a temp work dir; a relative --output-dir must not follow it there."""
    pytest.importorskip("openai")
    monkeypatch.chdir(tmp_path)
    dump = tmp_path / "argv.dump"
    nova = _fake_nova(tmp_path, f'printf "%s\\n" "$@" > {dump}; exit 3')
    code = run.main(
        ["--scenario", "service-incident-v1", "--variant", "unsafe", "--output-dir", "rel-out",
         "--nova-bin", str(nova)]
    )
    capsys.readouterr()
    assert code == 2
    args = dump.read_text().splitlines()
    for flag in ("--output-dir", "--scenario-dir", "--result-path"):
        value = args[args.index(flag) + 1]
        assert Path(value).is_absolute(), (flag, value)
    assert args[args.index("--output-dir") + 1] == str(tmp_path / "rel-out" / "capsules")
    assert (tmp_path / "rel-out" / "evidence-lab-report.json").is_file()


def test_harness_passes_a_sanitised_environment_to_nova(
    tmp_path: Path,
    guard_env: None,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("openai")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:3128")
    monkeypatch.setenv("NOVA_CAPTURE_LEVEL", "forensic")
    dump = tmp_path / "env.dump"
    nova = _fake_nova(tmp_path, f"env > {dump}; exit 3")
    _harness(tmp_path, "--nova-bin", str(nova), capsys=capsys)
    seen = dump.read_text()
    assert REAL_LOOKING_KEY not in seen
    assert "HTTPS_PROXY" not in seen and "NOVA_CAPTURE_LEVEL" not in seen
    assert f"OPENAI_API_KEY={lab.DUMMY_API_KEY}" in seen
    assert "OPENAI_BASE_URL=http://127.0.0.1:" in seen
    assert str(tmp_path / "guard-home") not in seen


def test_harness_turns_an_unexpected_internal_error_into_a_named_failure(
    tmp_path: Path, guard_env: None, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    pytest.importorskip("openai")

    def boom(*_args: object) -> None:
        raise RuntimeError("something nobody anticipated")

    monkeypatch.setattr(run, "_pipeline", boom)
    code, (report, captured) = _harness(tmp_path, capsys=capsys)
    assert code == 2
    assert "code=E_HARNESS_INTERNAL" in captured.err
    assert report["status"] == "error" and report["error"]["code"] == "E_HARNESS_INTERNAL"
    assert not Path(report["cleanup"]["temp_home"]).exists()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGHUP], ids=["sigterm", "sighup"])
def test_harness_cleans_up_when_it_is_killed_mid_capture(tmp_path: Path, signum: int) -> None:
    pytest.importorskip("openai")
    started, child = tmp_path / "started", tmp_path / "child-pid"
    nova = _fake_nova(tmp_path, f"touch {started}; sleep 300 & echo $! > {child}; wait")
    proc = subprocess.Popen(
        [sys.executable, str(LAB / "run.py"), "--scenario", "service-incident-v1", "--variant", "unsafe",
         "--output-dir", str(tmp_path / "out"), "--nova-bin", str(nova)],
        env=_isolated_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 60
        while not (started.exists() and child.exists()) and time.monotonic() < deadline:
            assert proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        assert started.exists(), "the harness never reached the capture step"
        proc.send_signal(signum)
        _, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 2 and "code=E_INTERRUPTED" in err
    report = json.loads((tmp_path / "out" / "evidence-lab-report.json").read_text())
    assert report["error"]["code"] == "E_INTERRUPTED"
    _assert_clean(report, tmp_path)
    assert _wait_dead(int(child.read_text())), "the capture's child outlived the harness"


def test_an_interrupt_lets_nova_capture_stop_a_workload_in_its_own_session(
    tmp_path: Path,
) -> None:  # fmt: skip
    """`nova capture` runs the workload in its own process group, so killing capture's group
    cannot reach it: capture must get a SIGTERM it can forward, not an immediate SIGKILL.
    The fake capture models that: its workload is in a NEW session and only the fake's TERM
    handler stops it."""
    pytest.importorskip("openai")
    started, child = tmp_path / "started", tmp_path / "child-pid"
    nova = _fake_nova(
        tmp_path,
        f"trap 'kill -TERM \"$(cat {child})\"; exit 143' TERM\n"
        f"setsid sleep 300 & echo $! > {child}\ntouch {started}\nwait",
    )
    proc = subprocess.Popen(
        [sys.executable, str(LAB / "run.py"), "--scenario", "service-incident-v1", "--variant", "unsafe",
         "--output-dir", str(tmp_path / "out"), "--nova-bin", str(nova)],
        env=_isolated_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    pid = None
    try:
        deadline = time.monotonic() + 60
        while not (started.exists() and child.exists()) and time.monotonic() < deadline:
            assert proc.poll() is None, proc.communicate()
            time.sleep(0.05)
        assert started.exists(), "the harness never reached the capture step"
        pid = int(child.read_text())
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
        assert proc.returncode == 2 and "code=E_INTERRUPTED" in err
        assert _wait_dead(pid), "the workload in its own session outlived the harness"
    finally:
        if proc.poll() is None:
            proc.kill()
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("kind", ["oversized", "fifo"])
def test_capsule_streams_are_read_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:  # fmt: skip
    path = tmp_path / "model-calls.jsonl"
    if kind == "fifo":
        os.mkfifo(path)  # an unbounded read of this would block forever
    else:
        path.write_text('{"a": 1}\n' * 10)
        monkeypatch.setattr(run, "MAX_CAPSULE_FILE_BYTES", 32)
    with pytest.raises(run.lab.EvidenceLabError) as exc:
        run._jsonl(path)
    assert exc.value.code == "E_CAPSULE_INVALID"


def test_harness_never_writes_bytecode_next_to_the_scripts(tmp_path: Path) -> None:
    pytest.importorskip("openai")
    lab_copy = tmp_path / "evidence_lab"
    shutil.copytree(LAB, lab_copy, ignore=shutil.ignore_patterns("__pycache__"))
    env = _isolated_env(tmp_path)
    env.pop("PYTHONDONTWRITEBYTECODE", None)  # the test must not be vacuous
    proc = subprocess.run(
        [sys.executable, str(lab_copy / "run.py"), "--scenario", "service-incident-v1", "--variant",
         "unsafe",
         "--output-dir", str(tmp_path / "out"), "--nova-bin", str(_fake_nova(tmp_path, "exit 3"))],
        env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert proc.returncode == 2, proc.stderr
    assert not list(lab_copy.rglob("__pycache__")), "bytecode was written beside the scripts"


# ── harness: the evidence judge, on hand-built capsule directories ──────────
#
# These feed ``run._evaluate`` small synthetic capsule *directories* to test the
# harness's own judgement (counting, ordering, privacy). They are inputs to a
# unit test, never presented as captured evidence — the real-capture tests below
# are the evidence.


def _fixture_result_digest(name: str) -> str:
    # A literal pin, not ``lab.expected_observations``: the judge under test computes its
    # expectation with that function, so building the capsule from the same function
    # would make the comparison agree with itself whatever the function returned.
    return "sha256:" + TOOL_RESULT_SHA256[name]


def _tool_record(name: str, **over: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "tool_name": name, "tool_version": "1", "transport": "python", "mutates": False,
        "mutation_class": "read-only", "status": "success", "arguments": {}, "result": None,
        "extensions": {"io.novafabric.result_digest": _fixture_result_digest(name)},
    }  # fmt: skip
    record.update(over)
    return record


BASE_URL = "http://127.0.0.1:45123/v1"


def _model_record(
    role: str | None = "logical", *, call_id: str = "CALL1", logical: str = "CALL1", **over: Any
) -> dict[str, Any]:
    """A model-calls.jsonl record shaped like the real ones (logical by default)."""
    golden = json.loads((SCENARIO_DIR / "golden" / "model_request.json").read_text())
    decision = json.loads((SCENARIO_DIR / "model_response.json").read_text())
    extensions: dict[str, Any] = {}
    if role:
        extensions = {
            "io.novafabric.record_role": role,
            "io.novafabric.logical_call_id": logical,
        }
    record: dict[str, Any] = {
        "model_call_id": call_id,
        "gen_ai.system": "openai",
        "gen_ai.request.model": MODEL_ID,
        "status": "success",
        "endpoint": BASE_URL + "/",
        "extensions": extensions,
    }
    if role != "transport":  # a wire record has neither the request text nor choices
        record["gen_ai.request.messages"] = golden["messages"]
        record["gen_ai.response.choices"] = [
            {
                "index": 0,
                "message": {"role": "assistant", "content": lab.canonical_json(decision)},
                "finish_reason": "stop",
            }
        ]
    record.update(over)
    return record


def _transport(**over: Any) -> dict[str, Any]:
    return _model_record("transport", call_id="T1", **over)


def _build_judged_run(
    tmp_path: Path, scenario: Any, *, models: list[dict[str, Any]], tools: list[dict[str, Any]],
    log_outcomes: tuple[str, ...] = ("accepted",),
) -> tuple[Path, dict[str, Any], Path]:  # fmt: skip
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    (capsule / "model-calls.jsonl").write_text("\n".join(json.dumps(r) for r in models) + "\n")
    (capsule / "tool-calls.jsonl").write_text("\n".join(json.dumps(r) for r in tools) + "\n")
    observations = lab.expected_observations(scenario)
    request_sha = lab.sha256_hex(lab.canonical_json(lab.build_request_body(scenario, observations)))
    decision = json.loads((SCENARIO_DIR / "model_response.json").read_text())
    agent_result = {
        "observation_sha256": {
            k: lab.sha256_hex(lab.canonical_json(v)) for k, v in observations.items()
        },
        "model": {"request_sha256": request_sha},
        "decision": decision,
        "decision_sha256": lab.sha256_hex(lab.canonical_json(decision)),
        "policy": {"result": "reject", "rule": "forbidden_action"},
    }
    log = tmp_path / "requests.jsonl"
    log.write_text(
        "\n".join(json.dumps({"outcome": o, "request_sha256": request_sha}) for o in log_outcomes)
        + "\n"
    )
    return capsule, agent_result, log


GOOD_TOOLS = ["read_metrics", "search_logs"]


def _judge(tmp_path: Path, scenario: Any, **kwargs: Any) -> Any:
    capsule, agent_result, log = _build_judged_run(tmp_path, scenario, **kwargs)
    return run._evaluate(scenario, capsule, agent_result, log, BASE_URL)


def test_judge_counts_only_logical_model_records(tmp_path: Path, scenario: Any) -> None:
    evaluated, checks = _judge(
        tmp_path, scenario,
        models=[_model_record("logical"), _model_record("transport"), _model_record("transport")],
        tools=[_tool_record(n) for n in GOOD_TOOLS],
    )  # fmt: skip
    assert evaluated["observed"]["logical_model_calls"] == 1
    assert evaluated["observed"]["transport_model_records"] == 2
    assert all(checks.values())


@pytest.mark.parametrize(
    ("models", "tools", "log", "code"),
    [
        ([], GOOD_TOOLS, ("accepted",), "E_COUNT_MISMATCH"),
        ([_model_record("transport")], GOOD_TOOLS, ("accepted",), "E_COUNT_MISMATCH"),
        (
            [_model_record("logical"), _model_record("logical")],
            GOOD_TOOLS, ("accepted",), "E_COUNT_MISMATCH",
        ),
        (
            [_model_record(), _model_record(None, call_id="CALL2", logical="CALL2")],
            GOOD_TOOLS, ("accepted",), "E_COUNT_MISMATCH",
        ),
        (
            [_model_record(), _model_record(None, call_id="X", endpoint="https://api.openai.com/v1")],
            GOOD_TOOLS, ("accepted",), "E_COUNT_MISMATCH",
        ),
        (
            [_model_record(), _transport(logical="SOME-OTHER-CALL")],
            GOOD_TOOLS, ("accepted",), "E_MODEL_RECORD_MISMATCH",
        ),
        (
            [_model_record(), _transport(endpoint="http://127.0.0.1:1/v1/")],
            GOOD_TOOLS, ("accepted",), "E_MODEL_RECORD_MISMATCH",
        ),
        (
            [_model_record(endpoint="http://127.0.0.1:45124/v1/")],
            GOOD_TOOLS, ("accepted",), "E_MODEL_RECORD_MISMATCH",
        ),
        (
            [_model_record(endpoint="http://127.0.0.1:45123@evil.example/v1/")],
            GOOD_TOOLS, ("accepted",), "E_MODEL_RECORD_MISMATCH",
        ),
        (
            [_model_record(**{"gen_ai.request.messages": [{"role": "user", "content": "x"}]})],
            GOOD_TOOLS, ("accepted",), "E_MODEL_RECORD_MISMATCH",
        ),
        (
            [_model_record(**{"gen_ai.response.choices": [{"index": 0, "finish_reason": "stop",
              "message": {"role": "assistant", "content": '{"action":"reduce_max_connections","value":5,"reason":"r"}'}}]})],
            GOOD_TOOLS, ("accepted",), "E_MODEL_RECORD_MISMATCH",
        ),
        ([_model_record()], GOOD_TOOLS, ("accepted", "error"), "E_MOCK_LOG_MISMATCH"),
        ([_model_record()], GOOD_TOOLS, ("error", "accepted"), "E_MOCK_LOG_MISMATCH"),
        ([_model_record()], ["read_metrics"], ("accepted",), "E_TOOL_ORDER_MISMATCH"),
        ([_model_record()], GOOD_TOOLS[::-1], ("accepted",), "E_TOOL_ORDER_MISMATCH"),
        ([_model_record()], GOOD_TOOLS + ["search_logs"], ("accepted",), "E_TOOL_ORDER_MISMATCH"),
        ([_model_record()], GOOD_TOOLS, (), "E_MOCK_LOG_MISMATCH"),
        ([_model_record()], GOOD_TOOLS, ("accepted", "rejected"), "E_MOCK_LOG_MISMATCH"),
        (
            [_model_record(**{"gen_ai.request.model": "gpt-4o-mini"})],
            GOOD_TOOLS, ("accepted",), "E_MODEL_RECORD_MISMATCH",
        ),
        (
            [_model_record(endpoint="https://api.openai.com/v1")],
            GOOD_TOOLS, ("accepted",), "E_MODEL_RECORD_MISMATCH",
        ),
    ],
    ids=[
        "no-model-record", "only-transport", "two-logical", "unmarked-extra-record",
        "unmarked-extra-non-loopback", "transport-of-another-call", "transport-other-endpoint",
        "other-loopback-port", "userinfo-endpoint", "request-differs-from-golden",
        "response-differs-from-fixture", "late-error-request", "early-error-request", "missing-tool",
        "wrong-order", "extra-tool", "no-mock-request", "extra-rejected-request", "wrong-model",
        "non-loopback-endpoint",
    ],
)  # fmt: skip
def test_judge_fails_on_missing_extra_or_wrong_records(
    tmp_path: Path, scenario: Any, models: list[dict[str, Any]], tools: list[str],
    log: tuple[str, ...], code: str,
) -> None:  # fmt: skip
    with pytest.raises(lab.EvidenceLabError) as exc:
        _judge(
            tmp_path,
            scenario,
            models=models,
            tools=[_tool_record(n) for n in tools],
            log_outcomes=log,
        )
    assert exc.value.code == code


def _scenario_copy(tmp_path: Path) -> Path:
    copy = tmp_path / "service_incident_v1"
    shutil.copytree(SCENARIO_DIR, copy)
    return copy


def test_judge_reads_the_committed_golden_transcript_not_only_the_builder(
    tmp_path: Path,
) -> None:
    """The check named 'matches the golden transcript' must compare with the golden file."""
    copy = _scenario_copy(tmp_path)
    golden_path = copy / "golden" / "model_request.json"
    golden = json.loads(golden_path.read_text())
    golden["messages"][0]["content"] += " (edited)"
    golden_path.write_text(json.dumps(golden))
    drifted = lab.load_scenario(copy)  # the manifest pins the four fixtures, not the golden
    (tmp_path / "run").mkdir()
    with pytest.raises(lab.EvidenceLabError) as exc:
        _judge(
            tmp_path / "run", drifted,
            models=[_model_record()],
            tools=[_tool_record(n) for n in GOOD_TOOLS],
        )  # fmt: skip
    assert exc.value.code == "E_MODEL_RECORD_MISMATCH"
    assert "golden" in str(exc.value)


def test_judge_compares_the_golden_transcript_by_canonical_bytes(tmp_path: Path) -> None:
    """42 and 42.0 are equal in Python but hash differently; the golden is a hashed artefact."""
    copy = _scenario_copy(tmp_path)
    golden_path = copy / "golden" / "model_request.json"
    golden = json.loads(golden_path.read_text())
    golden["seed"] = float(golden["seed"])
    golden_path.write_text(json.dumps(golden))
    (tmp_path / "run").mkdir()
    with pytest.raises(lab.EvidenceLabError) as exc:
        _judge(
            tmp_path / "run", lab.load_scenario(copy),
            models=[_model_record()],
            tools=[_tool_record(n) for n in GOOD_TOOLS],
        )  # fmt: skip
    assert exc.value.code == "E_MODEL_RECORD_MISMATCH"


def test_a_deeply_nested_golden_is_a_named_fixture_error(tmp_path: Path) -> None:
    copy = _scenario_copy(tmp_path)
    (copy / "golden" / "model_request.json").write_text("[" * 100_000 + "]" * 100_000)
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_golden_request(lab.load_scenario(copy))
    assert exc.value.code == "E_FIXTURE_INVALID"


def test_a_golden_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    """A symlink to a directory (or a device) must not be read; st_size cannot bound it."""
    copy = _scenario_copy(tmp_path)
    golden_path = copy / "golden" / "model_request.json"
    golden_path.unlink()
    golden_path.symlink_to(tmp_path)
    with pytest.raises(lab.EvidenceLabError) as exc:
        lab.load_golden_request(lab.load_scenario(copy))
    assert exc.value.code == "E_FIXTURE_INVALID"
    assert "regular file" in str(exc.value)


def test_judge_names_a_missing_golden_transcript(tmp_path: Path) -> None:
    copy = _scenario_copy(tmp_path)
    (copy / "golden" / "model_request.json").unlink()
    (tmp_path / "run").mkdir()
    with pytest.raises(lab.EvidenceLabError) as exc:
        _judge(
            tmp_path / "run", lab.load_scenario(copy),
            models=[_model_record()],
            tools=[_tool_record(n) for n in GOOD_TOOLS],
        )  # fmt: skip
    assert exc.value.code == "E_FIXTURE_MISSING"


@pytest.mark.parametrize("omitted", ["gen_ai.request.messages", "gen_ai.response.choices"])
def test_judge_fails_loudly_if_a_capture_default_ever_omits_the_model_text(
    tmp_path: Path, scenario: Any, omitted: str
) -> None:
    """The harness currently relies on the default capture level keeping the model text.

    If NovaFabric's capture default changes to omit it (an open owner decision this
    example must not pre-empt), the harness has to stop with a named failure rather than
    report a pass from a capsule that no longer holds the request or the response.
    """
    record = _model_record()
    del record[omitted]
    with pytest.raises(lab.EvidenceLabError) as exc:
        _judge(
            tmp_path,
            scenario,
            models=[record],
            tools=[_tool_record(n) for n in ("read_metrics", "search_logs")],
        )
    assert exc.value.code == "E_MODEL_RECORD_MISMATCH"


def test_judge_treats_a_single_unmarked_record_as_the_logical_call(
    tmp_path: Path, scenario: Any
) -> None:
    """ADR-0305: a model-call record without the role marker is a logical call."""
    evaluated, _ = _judge(
        tmp_path, scenario, models=[_model_record(None)],
        tools=[_tool_record(n) for n in GOOD_TOOLS],
    )  # fmt: skip
    assert evaluated["observed"]["logical_model_calls"] == 1


def test_judge_accepts_the_exact_endpoint_with_or_without_a_trailing_slash(
    tmp_path: Path, scenario: Any
) -> None:
    for i, endpoint in enumerate((BASE_URL, BASE_URL + "/")):
        sub = tmp_path / str(i)
        sub.mkdir()
        _judge(sub, scenario, models=[_model_record(endpoint=endpoint)],
               tools=[_tool_record(n) for n in GOOD_TOOLS])  # fmt: skip


@pytest.mark.parametrize(
    ("member", "content", "code"),
    [
        ("model-calls.jsonl", b'{"a": 1}\n{"truncated": ', "E_CAPSULE_INVALID"),
        ("model-calls.jsonl", b"\xff\xfe not utf-8\n", "E_CAPSULE_INVALID"),
        ("model-calls.jsonl", b"[]\n", "E_CAPSULE_INVALID"),
        ("tool-calls.jsonl", b'{"tool_name": "read_metrics"', "E_CAPSULE_INVALID"),
        ("tool-calls.jsonl", b'"just a string"\n', "E_CAPSULE_INVALID"),
        ("../requests.jsonl", b"not json\n", "E_MOCK_LOG_MISMATCH"),
    ],
    ids=["truncated", "non-utf8", "not-an-object", "tool-truncated", "tool-string", "bad-mock-log"],
)
def test_judge_names_malformed_evidence_files_instead_of_crashing(
    tmp_path: Path, scenario: Any, member: str, content: bytes, code: str
) -> None:
    capsule, agent_result, log = _build_judged_run(
        tmp_path, scenario, models=[_model_record()], tools=[_tool_record(n) for n in GOOD_TOOLS]
    )
    (capsule / member).write_bytes(content)
    with pytest.raises(lab.EvidenceLabError) as exc:
        run._evaluate(scenario, capsule, agent_result, log, BASE_URL)
    assert exc.value.code == code


def test_judge_names_a_missing_mock_log(tmp_path: Path, scenario: Any) -> None:
    capsule, agent_result, log = _build_judged_run(
        tmp_path, scenario, models=[_model_record()], tools=[_tool_record(n) for n in GOOD_TOOLS]
    )
    log.unlink()
    with pytest.raises(lab.EvidenceLabError) as exc:
        run._evaluate(scenario, capsule, agent_result, log, BASE_URL)
    assert exc.value.code == "E_MOCK_LOG_MISMATCH"


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"arguments": {"service": "payments"}}, "E_PRIVACY_VIOLATION"),
        ({"result": {"points": 8}}, "E_PRIVACY_VIOLATION"),
        ({"mutation_class": "unknown"}, "E_TOOL_RECORD_MISMATCH"),
        ({"mutates": True}, "E_TOOL_RECORD_MISMATCH"),
        ({"transport": "mcp"}, "E_TOOL_RECORD_MISMATCH"),
        ({"status": "error"}, "E_TOOL_RECORD_MISMATCH"),
        ({"tool_version": "2"}, "E_TOOL_RECORD_MISMATCH"),
    ],
)
def test_judge_fails_on_a_payload_or_a_non_read_only_tool_record(
    tmp_path: Path, scenario: Any, over: dict[str, Any], code: str
) -> None:
    tools = [_tool_record("read_metrics", **over), _tool_record("search_logs")]
    with pytest.raises(lab.EvidenceLabError) as exc:
        _judge(tmp_path, scenario, models=[_model_record()], tools=tools)
    assert exc.value.code == code


@pytest.mark.parametrize(
    "extensions",
    [
        {"io.novafabric.result_digest": "sha256:" + "0" * 64},
        {"io.novafabric.result_digest": "sha256:" + "ab" * 32},
        {},
    ],
    ids=["zero-digest", "other-digest", "no-digest"],
)
def test_judge_compares_each_recorded_result_digest_with_the_fixture(
    tmp_path: Path, scenario: Any, extensions: dict[str, str]
) -> None:
    tools = [_tool_record("read_metrics"), _tool_record("search_logs", extensions=extensions)]
    with pytest.raises(lab.EvidenceLabError) as exc:
        _judge(tmp_path, scenario, models=[_model_record()], tools=tools)
    assert exc.value.code == "E_OBSERVATION_MISMATCH"


def test_judge_fails_when_the_agent_reports_a_different_outcome(
    tmp_path: Path, scenario: Any
) -> None:
    capsule, result, log = _build_judged_run(
        tmp_path, scenario, models=[_model_record()], tools=[_tool_record(n) for n in GOOD_TOOLS]
    )
    safe = {"action": "reduce_max_connections", "value": 500, "reason": "r"}
    result.update(
        decision=safe, decision_sha256=lab.sha256_hex(lab.canonical_json(safe)),
        policy={"result": "accept", "rule": "allowlisted_action"},
    )  # fmt: skip
    with pytest.raises(lab.EvidenceLabError) as exc:
        run._evaluate(scenario, capsule, result, log, BASE_URL)
    assert exc.value.code == "E_UNEXPECTED_OUTCOME"
    result["policy"] = {"result": "reject", "rule": "forbidden_action"}  # lie about the oracle
    with pytest.raises(lab.EvidenceLabError) as exc:
        run._evaluate(scenario, capsule, result, log, BASE_URL)
    assert exc.value.code == "E_AGENT_RESULT_INVALID"
    result["observation_sha256"]["read_metrics"] = "0" * 64
    with pytest.raises(lab.EvidenceLabError) as exc:
        run._evaluate(scenario, capsule, result, log, BASE_URL)
    assert exc.value.code == "E_OBSERVATION_MISMATCH"


# ── real capture: the vertical slice ─────────────────────────────────────────


def needs_nova(fn: Any) -> Any:
    """Skip without the nova CLI; keep every real-capture test on one xdist worker.

    The ``d1`` fixture is module-scoped and runs two real captures, so under
    ``--dist=loadgroup`` it must not be rebuilt on each worker."""
    fn = pytest.mark.skipif(not NOVA.is_file(), reason="needs the installed nova console script")(
        fn
    )
    return pytest.mark.xdist_group("evidence-lab-d1")(fn)


def _run_harness_subprocess(
    tmp_path: Path, name: str, *, relative: bool = False
) -> tuple[subprocess.CompletedProcess[str], Path]:
    out = tmp_path / name
    env = _isolated_env(
        tmp_path,
        OPENAI_API_KEY=REAL_LOOKING_KEY,
        NOVA_CAPTURE_LEVEL="forensic",
        HTTPS_PROXY="http://127.0.0.1:9",
    )
    proc = subprocess.run(
        [sys.executable, str(LAB / "run.py"), "--scenario", "service-incident-v1", "--variant", "unsafe",
         "--output-dir", name if relative else str(out)],
        capture_output=True, text=True, env=env, cwd=tmp_path, timeout=300,
    )  # fmt: skip
    return proc, out


@pytest.fixture(scope="module")
def d1(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, Any]]:
    pytest.importorskip("openai")
    if not NOVA.is_file():
        pytest.skip("needs the installed nova console script")
    tmp_path = tmp_path_factory.mktemp("evidence-lab")
    runs = []
    for name, relative in (("run-a", False), ("run-b", True)):  # run-b: relative --output-dir
        runs.append(_run_harness_subprocess(tmp_path, name, relative=relative))
    yield {"tmp": tmp_path, "runs": runs}


def _report(out: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads((out / "evidence-lab-report.json").read_text())
    return parsed


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@needs_nova
def test_d1_harness_exits_zero_and_states_both_verdicts(d1: dict[str, Any]) -> None:
    proc, _ = d1["runs"][0]
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "task verdict: FAIL" in proc.stdout
    assert "disable_rate_limiting" in proc.stdout
    assert "harness verdict: PASS" in proc.stdout


@needs_nova
def test_d1_output_labels_input_capture_and_every_verdict(d1: dict[str, Any]) -> None:
    """A reader of the output alone can tell what is synthetic, what is real, what is unproven."""
    proc, out = d1["runs"][0]
    lines = proc.stdout.splitlines()
    by_prefix = {line.split(":", 1)[0]: line for line in lines if ":" in line}
    assert "SYNTHETIC" in by_prefix["input"] and "no real data" in by_prefix["input"]
    assert "actual `nova capture`" in by_prefix["capture"]
    assert "NovaFabric" in by_prefix["capture"]
    # the four verdict axes, each on its own labelled line, none merged into another
    assert by_prefix["task verdict"].startswith("task verdict: FAIL")
    assert by_prefix["harness verdict"].startswith("harness verdict: PASS")
    last = lines[-1]
    assert last.startswith("integrity: NOT_EVALUATED") and "replay: NOT_EVALUATED" in last
    report = _report(out)
    assert report["input"]["kind"] == "synthetic" and report["input"]["real_data"] is False
    assert report["capture"]["kind"] == "actual-nova-capture"
    assert report["capture"]["novafabric_version"] == report["novafabric_version"]
    assert report["capture"]["injected_records"] is False
    assert report["axes"]["task"]["verdict"] == "FAIL"
    assert report["harness_verdict"] == "PASS"


@needs_nova
def test_d1_real_capsule_validates_with_the_real_cli(d1: dict[str, Any]) -> None:
    _, out = d1["runs"][0]
    report = _report(out)
    capsule = out / "capsules" / report["run_id"]
    assert capsule.is_dir() and (capsule / "capsule.yaml").is_file()
    checked = subprocess.run(
        [str(NOVA), "validate", str(capsule)],
        capture_output=True,
        text=True,
        timeout=120,
        env=_isolated_env(d1["tmp"], NOVAFABRIC_HOME=str(d1["tmp"] / "validate-home")),
    )
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert report["commands"]["validate"]["exit_code"] == 0
    assert report["commands"]["capture"]["exit_code"] == 0


@needs_nova
def test_d1_capsule_holds_one_logical_model_call_and_two_read_only_tool_calls(
    d1: dict[str, Any],
) -> None:
    _, out = d1["runs"][0]
    report = _report(out)
    capsule = out / "capsules" / report["run_id"]
    model_calls = _jsonl(capsule / "model-calls.jsonl")
    logical = [
        r
        for r in model_calls
        if r.get("extensions", {}).get("io.novafabric.record_role") == "logical"
    ]
    assert len(logical) == 1
    assert logical[0]["gen_ai.request.model"] == MODEL_ID
    assert logical[0]["gen_ai.system"] == "openai"
    assert logical[0]["endpoint"].startswith("http://127.0.0.1:")
    assert logical[0]["status"] == "success"
    tools = _jsonl(capsule / "tool-calls.jsonl")
    assert [t["tool_name"] for t in tools] == ["read_metrics", "search_logs"]
    for record in tools:
        assert record["transport"] == "python"
        assert record["mutation_class"] == "read-only" and record["mutates"] is False
        assert record["status"] == "success"
    observed = report["observed"]
    assert observed["logical_model_calls"] == 1
    assert observed["tool_calls"] == 2
    assert observed["tool_sequence"] == ["read_metrics", "search_logs"]
    assert observed["transport_model_records"] == len(model_calls) - len(logical)
    assert report["expected"]["logical_model_calls"] == 1
    assert report["expected"]["tool_calls"] == 2


@needs_nova
def test_d1_tool_capture_stays_at_the_default_digest_only_level(d1: dict[str, Any]) -> None:
    _, out = d1["runs"][0]
    capsule = out / "capsules" / _report(out)["run_id"]
    for record in _jsonl(capsule / "tool-calls.jsonl"):
        assert record["arguments"] == {} and record["result"] is None
        ext = record["extensions"]
        assert ext["io.novafabric.arguments_digest"] and ext["io.novafabric.result_digest"]
        assert ext["io.novafabric.result_codec"] == "not-servable"
    blob = b"".join(p.read_bytes() for p in (capsule / "tool-calls.jsonl",))
    assert b"connection pool exhausted" not in blob  # log text is not in tool records


@needs_nova
def test_d1_policy_oracle_rejection_is_observed_and_reported(d1: dict[str, Any]) -> None:
    _, out = d1["runs"][0]
    report = _report(out)
    result = json.loads((out / "agent-result.json").read_text())
    assert result["decision"]["action"] == "disable_rate_limiting"
    assert result["policy"]["result"] == "reject"
    assert report["task"] == {
        "verdict": "FAIL",
        "reason": "policy oracle rejected 'disable_rate_limiting' (forbidden_action)",
        "expected_unsafe_outcome": True,
        "expected_verdict": "FAIL",
    }
    assert report["harness_verdict"] == "PASS"
    assert report["status"] == "ok"
    log = _jsonl(out / "mock-requests.jsonl")
    assert [e["outcome"] for e in log] == ["accepted"]


@needs_nova
def test_d1_repeat_runs_share_canonical_hashes_and_differ_only_in_ephemera(
    d1: dict[str, Any],
) -> None:
    (proc_a, out_a), (proc_b, out_b) = d1["runs"]
    assert proc_a.returncode == 0 and proc_b.returncode == 0
    a, b = _report(out_a), _report(out_b)
    assert a["canonical"] == b["canonical"]
    assert a["canonical"]["request_sha256"] == GOLDEN_REQUEST_SHA256
    assert a["canonical"]["fixture_sha256"] == FIXTURE_SHA256
    assert a["run_id"] != b["run_id"]
    for key in ("fixture_manifest_sha256", "tool_sequence_sha256", "decision_sha256"):
        assert len(a["canonical"][key]) == 64


@needs_nova
def test_d1_report_carries_the_evidence_fields(d1: dict[str, Any]) -> None:
    _, out = d1["runs"][0]
    report = _report(out)
    for key in (
        "schema_revision",
        "scenario_id",
        "seed",
        "profile",
        "novafabric_version",
        "python_version",
        "openai_version",
        "run_id",
        "capsule_path",
        "commands",
        "expected",
        "observed",
        "canonical",
        "task",
        "harness_verdict",
        "cleanup",
        "elapsed_seconds",
        "checks",
    ):
        assert key in report, key
    assert report["scenario_id"] == "service-incident-v1" and report["seed"] == 42
    assert report["profile"] == "zero-key"
    assert all(report["checks"].values()), report["checks"]
    assert report["axes"]["integrity"]["verdict"] == "NOT_EVALUATED"
    assert report["axes"]["replay"]["verdict"] == "NOT_EVALUATED"


@needs_nova
def test_d1_real_secret_and_user_state_never_reach_the_evidence(d1: dict[str, Any]) -> None:
    for _, out in d1["runs"]:
        for path in out.rglob("*"):
            if path.is_file():
                assert REAL_LOOKING_KEY.encode() not in path.read_bytes(), path
        report = _report(out)
        _assert_clean(report, d1["tmp"])
    assert os.listdir(d1["tmp"] / "guard-home") == []


# ── public clone / packaging hygiene ─────────────────────────────────────────


def test_every_lab_file_is_visible_to_a_public_clone() -> None:
    """Nothing the example needs may be git-ignored (and so absent from a clone)."""
    if shutil.which("git") is None or not (REPO / ".git").exists():
        pytest.skip("needs a git checkout")
    files = [p for p in LAB.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert files, "no lab files found"
    rels = [str(p.relative_to(REPO)) for p in files]
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", *rels], cwd=REPO, capture_output=True, text=True
    )
    assert ignored.returncode == 1 and ignored.stdout == "", f"git-ignored: {ignored.stdout}"


def test_the_lab_never_imports_anything_private_or_outside_the_repo() -> None:
    for path in LAB.rglob("*.py"):
        text = path.read_text()
        for needle in ("/home/", "design/", ".claude", "papers/", "novafabric.private"):
            assert needle not in text, f"{path.name} mentions {needle!r}"


def test_the_blackbox_demo_entry_point_is_untouched() -> None:
    demo = REPO / "examples" / "blackbox_demo"
    assert (demo / "run_demo.sh").is_file() and (demo / "agent.py").is_file()
    assert "X-Demo-Mode" in (demo / "mock_llm_server.py").read_text()
