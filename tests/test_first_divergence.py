"""Issue #74 — ``nova diff`` and ``nova verify`` name the first divergent step.

diff: the earliest changed/added/removed model or tool call, with its step
index in each run. verify: the first evidence file the seal no longer matches —
file granularity only, because the seal holds one sha256 per file, so the
record index is never claimed (``index`` is null).
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture.orchestrator import _evidence_digests
from novafabric.cli.main import app
from novafabric.diff._engine import DiffEngine
from novafabric.trust.novaseal import KeyConfig, NovaSeal
from novafabric.trust.novaseal.capsule_verify import CapsuleBinding
from novafabric.trust.novaseal.first_divergence import binding_first_divergence

runner = CliRunner()
ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ["src/novafabric/schemas/diff-report.schema.json", "schemas/diff-report.schema.json"]


# ---------------------------------------------------------------- diff fixtures


def _capsule(tmp_path: Path, name: str, models: list[dict], tools: list[dict]) -> Path:
    cap = tmp_path / name
    (cap / "outputs").mkdir(parents=True)
    (cap / "capsule.yaml").write_text(yaml.dump({"run_id": name, "status": "success"}))
    (cap / "env.lock").write_text(yaml.dump({"python": {"version": "3.12.3"}}))
    (cap / "model-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in models))
    (cap / "tool-calls.jsonl").write_text("".join(json.dumps(r) + "\n" for r in tools))
    (cap / "outputs" / "stdout.txt").write_text("hello\n")
    return cap


def _model(n: int, prompt: str, answer: str = "ok", second: int | None = None) -> dict:
    record: dict[str, Any] = {
        "model_call_id": f"MC-{n}",
        "parent_span_id": "root",
        "gen_ai.system": "openai",
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.request.messages": [{"role": "user", "content": prompt}],
        "gen_ai.response.choices": [{"index": 0, "message": {"content": answer}}],
    }
    if second is not None:
        record["started_at"] = f"2026-11-21T10:00:{second:02d}.000000Z"
    return record


def _tool(n: int, name: str, result: str = "r", second: int | None = None) -> dict:
    record: dict[str, Any] = {
        "tool_call_id": f"TC-{n}",
        "tool_name": name,
        "arguments": {"q": name},
        "result": result,
        "status": "ok",
    }
    if second is not None:
        record["started_at"] = f"2026-11-21T10:00:{second:02d}.000000Z"
    return record


def _diff_json(a: Path, b: Path) -> dict[str, Any]:
    result = runner.invoke(app, ["diff", str(a), str(b), "--output-format", "json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# ---------------------------------------------------------------- diff tests


def test_benign_divergence_names_the_first_changed_call(tmp_path: Path) -> None:
    """Timeline: model(0) tool(1) model(2) tool(3); B's step-2 answer differs."""
    a = _capsule(
        tmp_path, "a",
        [_model(1, "p1", second=0), _model(2, "p2", "yes", second=2)],
        [_tool(1, "search", second=1), _tool(2, "fetch", second=3)],
    )
    b = _capsule(
        tmp_path, "b",
        [_model(1, "p1", second=0), _model(2, "p2", "no", second=2)],
        [_tool(1, "search", second=1), _tool(2, "fetch", "other", second=3)],
    )
    fd = _diff_json(a, b)["first_divergence"]
    assert fd == {
        "index": 2,
        "index_a": 2,
        "index_b": 2,
        "stream": "model_calls",
        "event_type": "model_call",
        "reason": "changed",
        "aspects": ["response"],
        "record_id_a": "MC-2",
        "record_id_b": "MC-2",
        "timeline": "started_at",
    }

    text = runner.invoke(app, ["diff", str(a), str(b)])
    assert text.exit_code == 0
    assert "First divergence: step 2 model_call changed (response)" in text.stdout
    assert "[A #2 MC-2; B #2 MC-2]" in text.stdout


def test_an_earlier_tool_call_beats_a_later_model_call(tmp_path: Path) -> None:
    a = _capsule(
        tmp_path, "a", [_model(1, "p1", "x", second=5)], [_tool(1, "search", "r1", second=1)]
    )
    b = _capsule(
        tmp_path, "b", [_model(1, "p1", "y", second=5)], [_tool(1, "search", "r2", second=1)]
    )
    fd = _diff_json(a, b)["first_divergence"]
    assert (fd["index"], fd["event_type"], fd["aspects"]) == (0, "tool_call", ["result"])


def test_a_call_added_in_b_has_no_a_index(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [_model(1, "p1", second=0)], [])
    b = _capsule(tmp_path, "b", [_model(1, "p1", second=0), _model(2, "new", second=1)], [])
    fd = _diff_json(a, b)["first_divergence"]
    assert fd["reason"] == "added"
    assert (fd["index"], fd["index_a"], fd["index_b"]) == (1, None, 1)
    assert fd["record_id_b"] == "MC-2"


def test_without_started_at_the_index_is_per_stream(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [_model(1, "p1"), _model(2, "p2", "x")], [_tool(1, "s")])
    b = _capsule(tmp_path, "b", [_model(1, "p1"), _model(2, "p2", "y")], [_tool(1, "s")])
    fd = _diff_json(a, b)["first_divergence"]
    assert (fd["timeline"], fd["stream"], fd["index"]) == ("per_stream", "model_calls", 1)
    text = runner.invoke(app, ["diff", str(a), str(b)]).stdout
    assert "step counts model_calls records only" in text


def test_identical_runs_have_a_null_first_divergence(tmp_path: Path) -> None:
    calls = [_model(1, "p1", second=0)]
    a = _capsule(tmp_path, "a", calls, [_tool(1, "s", second=1)])
    b = _capsule(tmp_path, "b", calls, [_tool(1, "s", second=1)])
    doc = _diff_json(a, b)
    assert "first_divergence" in doc and doc["first_divergence"] is None
    text = runner.invoke(app, ["diff", str(a), str(b)]).stdout
    assert "No differences found." in text
    assert "First divergence" not in text


def test_an_output_only_change_is_not_an_event_divergence(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [_model(1, "p1")], [])
    b = _capsule(tmp_path, "b", [_model(1, "p1")], [])
    (b / "outputs" / "stdout.txt").write_text("changed\n")
    assert _diff_json(a, b)["first_divergence"] is None
    gate = runner.invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert gate.exit_code == 1 and isinstance(gate.exception, SystemExit)
    assert "First divergence: none in model or tool calls" in gate.stdout


def test_gate_exit_codes_are_unchanged(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [_model(1, "p1", "x")], [])
    b = _capsule(tmp_path, "b", [_model(1, "p1", "y")], [])
    gate = runner.invoke(app, ["diff", str(a), str(b), "--assert-no-regressions"])
    assert gate.exit_code == 1 and isinstance(gate.exception, SystemExit)
    same = runner.invoke(app, ["diff", str(a), str(a), "--assert-no-regressions"])
    assert same.exit_code == 0


@pytest.mark.parametrize("schema_path", SCHEMAS)
def test_first_divergence_validates_against_both_schemas(
    tmp_path: Path, schema_path: str
) -> None:
    schema = json.loads((ROOT / schema_path).read_text())
    assert "first_divergence" in schema["properties"]
    validator = jsonschema.Draft202012Validator(schema)
    a = _capsule(tmp_path, "a", [_model(1, "p1", "x", second=0)], [])
    b = _capsule(tmp_path, "b", [_model(1, "p1", "y", second=0)], [])
    validator.validate(_diff_json(a, b))
    validator.validate(_diff_json(a, a))
    bad = dict(_diff_json(a, b))
    bad["first_divergence"] = {**bad["first_divergence"], "index": -1}
    assert not validator.is_valid(bad)


def test_engine_report_carries_the_field(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [], [_tool(1, "s")])
    b = _capsule(tmp_path, "b", [], [])
    report = DiffEngine().compare(a, b)
    assert report.first_divergence is not None
    assert report.first_divergence["reason"] == "removed"
    assert report.first_divergence["index_b"] is None


# ---------------------------------------------------------------- verify fixture


@pytest.fixture()
def sealed(tmp_path: Path) -> tuple[Path, Path]:
    """A capsule sealed with evidence_digests, as `nova capture` seals one."""
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "issue-74")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    merkle_db = tmp_path / "merkle.db"
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {merkle_db}\n"
    )

    cap = tmp_path / "capsule-74"
    (cap / "outputs").mkdir(parents=True)
    (cap / "model-calls.jsonl").write_text('{"model":"gpt-4o","tokens_in":77}\n')
    (cap / "trace.jsonl").write_text('{"span":"root"}\n')
    (cap / "env.lock").write_text("python=3.13\n")
    (cap / "outputs" / "stdout.txt").write_text("hello\n")
    manifest: dict[str, Any] = {
        "run_id": "issue-74",
        "status": "success",
        "model_calls_ref": "model-calls.jsonl",
        "trace_ref": "trace.jsonl",
        "environment_ref": "env.lock",
    }
    manifest["evidence_digests"] = _evidence_digests(cap)
    (cap / "capsule.yaml").write_text(yaml.dump(manifest))
    seal = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="",
        db_path=str(merkle_db),
    )
    bundle = seal.seal(manifest)
    (cap / ".seal").mkdir()
    (cap / ".seal" / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (cap / ".seal" / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (cap / ".seal" / "log-entry.json").write_text(json.dumps(bundle.log_entry))
    return cap, config_path


def _verify(cap: Path, config: Path, *extra: str) -> Any:
    return runner.invoke(app, ["verify", str(cap), "--seal-config", str(config), *extra])


# ---------------------------------------------------------------- verify tests


def test_untampered_capsule_has_null_first_divergence(sealed: tuple[Path, Path]) -> None:
    cap, config = sealed
    result = _verify(cap, config, "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["valid"] is True
    assert "first_divergence" in doc and doc["first_divergence"] is None
    assert "First divergence" not in _verify(cap, config).stdout


def test_tampered_record_stream_is_named_at_file_granularity(
    sealed: tuple[Path, Path],
) -> None:
    cap, config = sealed
    with (cap / "model-calls.jsonl").open("a") as fh:
        fh.write('{"model":"gpt-4o","tokens_in":1}\n')  # a fabricated call

    result = _verify(cap, config, "--json")
    assert result.exit_code == 1 and isinstance(result.exception, SystemExit)
    doc = json.loads(result.stdout)
    assert doc["valid"] is False
    assert doc["first_divergence"] == {
        "path": "model-calls.jsonl",
        "stream": "model_calls",
        "event_type": "model_call",
        "index": None,
        "reason": "modified",
        "granularity": "file",
        "divergent_files": 1,
    }

    text = _verify(cap, config)
    assert text.exit_code == 1 and isinstance(text.exception, SystemExit)
    assert "First divergence: model-calls.jsonl (model_call records) modified" in text.stdout
    assert "record index not locatable" in text.stdout


def test_an_event_stream_is_named_before_other_files(sealed: tuple[Path, Path]) -> None:
    cap, config = sealed
    (cap / "env.lock").write_text("python=3.99\n")  # sorts before trace.jsonl
    (cap / "trace.jsonl").write_text('{"span":"forged"}\n')
    doc = json.loads(_verify(cap, config, "--json").stdout)
    fd = doc["first_divergence"]
    assert (fd["path"], fd["event_type"], fd["divergent_files"]) == ("trace.jsonl", "span", 2)


def test_a_deleted_stream_is_reported_missing(sealed: tuple[Path, Path]) -> None:
    cap, config = sealed
    (cap / "outputs" / "stdout.txt").unlink()
    fd = json.loads(_verify(cap, config, "--json").stdout)["first_divergence"]
    assert (fd["path"], fd["reason"], fd["event_type"]) == ("outputs/stdout.txt", "missing", None)


def test_no_digests_means_nothing_to_name() -> None:
    assert binding_first_divergence(CapsuleBinding(mismatched=("x",))) is None
    assert binding_first_divergence(CapsuleBinding(digests_present=True)) is None
