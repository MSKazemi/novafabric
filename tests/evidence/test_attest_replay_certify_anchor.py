"""Spine-B2 (ADR-0094) — ``nova evidence attest-replay --certify/--anchor``.

Both flags are additive: without them the command's behavior and outputs are
unchanged (the compat invariant), ``--certify`` emits the DSSE-signed
determinism certificate via the shipped ``replay_attestation`` module, and
``--anchor`` seals the attestation digest into the shipped adversary-anchored
ledger (per-stream sidecar hash chain + signed checkpoint).
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from novafabric.cli.evidence import evidence_app
from novafabric.cli.ledger import ledger_app
from novafabric.evidence.replay_attestation import (
    classify_match_verdict,
    pinned_block_from_capsule,
)
from novafabric.evidence.signing import generate_keypair
from novafabric.replay._result import ReplayResult

runner = CliRunner()

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "schemas" / "replay-attestation.schema.json"
)


def _capsule(tmp_path: Path, *, pinned: bool = False) -> Path:
    capsule = tmp_path / "run-b2"
    capsule.mkdir(parents=True)
    (capsule / "capsule.yaml").write_text("run_id: run-b2\n")
    record: dict[str, object] = {
        "gen_ai.request.model": "llama-3.1-8b",
        "gen_ai.response.model": "llama-3.1-8b",
    }
    if pinned:
        record.update(
            {
                "gen_ai.request.seed": 42,
                "gen_ai.request.temperature": 0.0,
                "gen_ai.request.top_p": 1.0,
                "model_digest": "sha256:" + "2c" * 32,
            }
        )
    (capsule / "model-calls.jsonl").write_text(json.dumps(record) + "\n")
    (capsule / "tool-calls.jsonl").write_text(json.dumps({"tool_name": "db"}) + "\n")
    if pinned:
        (capsule / "env.lock").write_text(
            yaml.dump(
                {
                    "mode": "deterministic",
                    "container": {"image_digest": "sha256:" + "fc" * 32},
                    "python": {"lock_file_hash": "sha256:" + "e3" * 32},
                    "hardware": {"inference": {"deterministic": True}},
                }
            )
        )
    return capsule


class _FakeEngine:
    """Replay-engine stand-in — returns a canned :class:`ReplayResult`."""

    status = "success"

    def __init__(self, *, capsule_dir: Path, flags: object, base_dir: Path) -> None:
        self._capsule_dir = capsule_dir
        self._mode = getattr(flags, "mode", "mocked")

    def run(self) -> ReplayResult:
        return ReplayResult(
            replay_id="rp-b2",
            replay_of_run_id=self._capsule_dir.name,
            mode=self._mode,
            status=self.status,
            start_time="2026-07-15T08:00:00Z",
            end_time="2026-07-15T08:00:05Z",
            duration_ms=5000,
            policy_flags_used=[],
            env_warnings=[],
        )


@pytest.fixture()
def fake_engine(monkeypatch: pytest.MonkeyPatch) -> type[_FakeEngine]:
    class Engine(_FakeEngine):
        pass

    monkeypatch.setattr("novafabric.replay._engine.ReplayEngine", Engine)
    return Engine


@pytest.fixture()
def keys(tmp_path: Path) -> tuple[Path, Path]:
    return generate_keypair(tmp_path / "keys")


def _invoke(capsule: Path, key: Path, out: Path, *extra: str) -> object:
    return runner.invoke(
        evidence_app,
        ["attest-replay", str(capsule), "--key", str(key), "-o", str(out), *extra],
    )


def _predicate_of(envelope_path: Path) -> dict:
    envelope = json.loads(envelope_path.read_text())
    statement = json.loads(base64.b64decode(envelope["payload"]).decode())
    assert statement["predicateType"], statement
    return {"type": statement["predicateType"], **statement["predicate"]}


# --- compat: without flags, behavior is unchanged -----------------------------


def test_without_flags_output_unchanged(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path)
    before = sorted(p.name for p in capsule.iterdir())
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out)
    assert res.exit_code == 0, res.output
    # only the re-performance envelope is written; the capsule is untouched
    assert out.exists()
    assert sorted(p.name for p in capsule.iterdir()) == before
    assert not (capsule / ".ledger").exists()
    assert not (capsule / "attestations.jsonl").exists()
    assert not list(tmp_path.glob("replay-attestation-*.intoto.json"))
    predicate = _predicate_of(out)
    assert predicate["type"] == "https://novafabric.io/reperformance/v0"
    assert "determinism_class" not in predicate


# --- --certify -----------------------------------------------------------------


def test_certify_underpinned_run_is_non_deterministic(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path, pinned=False)
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--certify")
    assert res.exit_code == 0, res.output
    cert_path = tmp_path / "replay-attestation-run-b2.intoto.json"
    assert cert_path.exists()
    cert = _predicate_of(cert_path)
    assert cert["type"] == "https://novafabric.io/replay-attestation/v0"
    assert cert["determinism_class"] == "NON_DETERMINISTIC"
    assert cert["reasons"]  # honest downgrade reasons recorded
    # superset invariant: base re-performance fields carried unchanged
    base = _predicate_of(out)
    for field in ("run_id", "capsule_hash", "outcome_digest", "match", "replay_mode"):
        assert cert[field] == base[field]
    # no anchor requested → no ledger, no ledger_ref
    assert not (capsule / ".ledger").exists()
    assert "ledger_ref" not in cert


def test_certify_exact_fully_pinned_is_bit_exact(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path, pinned=True)
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--mode", "exact", "--certify")
    assert res.exit_code == 0, res.output
    cert = _predicate_of(tmp_path / "replay-attestation-run-b2.intoto.json")
    assert cert["match"] == "exact"
    assert cert["determinism_class"] == "BIT_EXACT"
    assert cert["pinned"]["model"]["seed"] == 42
    assert cert["pinned"]["env"]["lock_mode"] == "deterministic"


def test_certificate_validates_against_schema(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path, pinned=True)
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--mode", "exact", "--certify", "--anchor")
    assert res.exit_code == 0, res.output
    cert = _predicate_of(tmp_path / "replay-attestation-run-b2.intoto.json")
    cert.pop("type")
    schema = json.loads(_SCHEMA_PATH.read_text())
    Draft202012Validator(schema).validate(cert)


def test_certify_on_mismatch_still_exits_2_with_certificate(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    fake_engine.status = "failure"
    capsule = _capsule(tmp_path, pinned=True)
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--certify")
    assert res.exit_code == 2, res.output  # legacy exit code preserved
    cert = _predicate_of(tmp_path / "replay-attestation-run-b2.intoto.json")
    assert cert["match"] == "mismatch"
    assert cert["determinism_class"] == "NON_DETERMINISTIC"


# --- --anchor --------------------------------------------------------------


def test_anchor_seals_attestation_digest_and_ledger_verifies(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path)
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--anchor")
    assert res.exit_code == 0, res.output

    stream = capsule / "attestations.jsonl"
    assert stream.exists()
    record = json.loads(stream.read_text().splitlines()[0])
    assert record["attestation_sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert record["attestation_file"] == out.name
    assert (capsule / ".ledger" / "attestations.chain.json").exists()
    assert (capsule / ".ledger" / "checkpoint.json").exists()

    verify = runner.invoke(
        ledger_app, ["verify", str(capsule), "--pubkey", str(keys[1])]
    )
    assert verify.exit_code == 0, verify.output


def test_anchor_makes_attestation_stream_tamper_evident(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path)
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--anchor")
    assert res.exit_code == 0, res.output
    # adversary rewrites the anchored digest record
    (capsule / "attestations.jsonl").write_text(
        json.dumps({"attestation_sha256": "0" * 64}) + "\n"
    )
    verify = runner.invoke(
        ledger_app, ["verify", str(capsule), "--pubkey", str(keys[1])]
    )
    assert verify.exit_code == 3  # content edit (ADR-0094 taxonomy)


def test_certify_with_anchor_fills_ledger_ref(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path)
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--certify", "--anchor")
    assert res.exit_code == 0, res.output
    cert = _predicate_of(tmp_path / "replay-attestation-run-b2.intoto.json")
    checkpoint = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    assert cert["ledger_ref"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert cert["ledger_ref"]["capsule_id"] == capsule.name


def test_certify_backlinks_preexisting_checkpoint(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path)
    anchored = runner.invoke(
        ledger_app, ["anchor", str(capsule), "--key", str(keys[0])]
    )
    assert anchored.exit_code == 0, anchored.output
    checkpoint = json.loads((capsule / ".ledger" / "checkpoint.json").read_text())
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--certify")
    assert res.exit_code == 0, res.output
    cert = _predicate_of(tmp_path / "replay-attestation-run-b2.intoto.json")
    assert cert["ledger_ref"]["checkpoint_id"] == checkpoint["checkpoint_id"]


def test_anchor_appends_across_repeat_runs(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path)
    out = tmp_path / "reperformance.intoto.json"
    assert _invoke(capsule, keys[0], out, "--anchor").exit_code == 0
    assert _invoke(capsule, keys[0], out, "--anchor").exit_code == 0
    lines = (capsule / "attestations.jsonl").read_text().splitlines()
    assert len(lines) == 2
    # the re-anchored ledger still verifies (chain + checkpoint rebuilt)
    verify = runner.invoke(
        ledger_app, ["verify", str(capsule), "--pubkey", str(keys[1])]
    )
    assert verify.exit_code == 0, verify.output


# --- helper units ------------------------------------------------------------


def test_pinned_block_from_capsule_extracts_pins(tmp_path: Path) -> None:
    capsule = _capsule(tmp_path, pinned=True)
    pinned = pinned_block_from_capsule(capsule)
    assert pinned.model.request_model == "llama-3.1-8b"
    assert pinned.model.seed == 42
    assert pinned.model.model_digest == "sha256:" + "2c" * 32
    assert pinned.env.lock_mode == "deterministic"
    assert pinned.env.container_image_digest == "sha256:" + "fc" * 32
    assert pinned.env.python_lock_file_hash == "sha256:" + "e3" * 32
    assert pinned.env.inference_deterministic is True


def test_pinned_block_missing_files_never_fabricates(tmp_path: Path) -> None:
    capsule = tmp_path / "bare"
    capsule.mkdir()
    pinned = pinned_block_from_capsule(capsule)
    assert pinned.model.request_model == "unknown"
    assert pinned.model.seed is None
    assert pinned.model.model_digest is None
    assert pinned.env.lock_mode == "best-effort"
    assert pinned.env.container_image_digest is None


def test_pinned_block_tolerates_malformed_inputs(tmp_path: Path) -> None:
    capsule = tmp_path / "bad"
    capsule.mkdir()
    (capsule / "model-calls.jsonl").write_text("\nnot json\n")
    (capsule / "env.lock").write_text("mode: [unclosed\n")
    pinned = pinned_block_from_capsule(capsule)
    assert pinned.model.request_model == "unknown"
    assert pinned.env.lock_mode == "best-effort"

    (capsule / "env.lock").write_text("- just\n- a list\n")
    assert pinned_block_from_capsule(capsule).env.lock_mode == "best-effort"


def test_certify_ignores_corrupt_checkpoint(
    tmp_path: Path, fake_engine: type[_FakeEngine], keys: tuple[Path, Path]
) -> None:
    capsule = _capsule(tmp_path)
    ledger_dir = capsule / ".ledger"
    ledger_dir.mkdir()
    (ledger_dir / "checkpoint.json").write_text("not json")
    out = tmp_path / "reperformance.intoto.json"
    res = _invoke(capsule, keys[0], out, "--certify")
    assert res.exit_code == 0, res.output
    cert = _predicate_of(tmp_path / "replay-attestation-run-b2.intoto.json")
    assert "ledger_ref" not in cert  # corrupt checkpoint → honestly omitted

    (ledger_dir / "checkpoint.json").write_text("{}")
    res = _invoke(capsule, keys[0], out, "--certify")
    assert res.exit_code == 0, res.output
    cert = _predicate_of(tmp_path / "replay-attestation-run-b2.intoto.json")
    assert "ledger_ref" not in cert  # checkpoint without an id → omitted


def test_classify_match_verdict_downgrade_rule(tmp_path: Path) -> None:
    fully = pinned_block_from_capsule(_capsule(tmp_path, pinned=True))
    cls, reasons = classify_match_verdict("exact", fully)
    assert cls == "BIT_EXACT"
    assert reasons == []
    cls, reasons = classify_match_verdict("semantic-match", fully)
    assert cls == "NON_DETERMINISTIC"
    assert any("not exact" in r for r in reasons)
    under = pinned_block_from_capsule(_capsule(tmp_path / "u", pinned=False))
    cls, reasons = classify_match_verdict("exact", under)
    assert cls == "NON_DETERMINISTIC"
    assert any("missing required pins" in r for r in reasons)


def test_anchor_capsule_requires_streams(tmp_path: Path) -> None:
    from novafabric.trust.ledger import anchor_capsule

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no .jsonl evidence streams"):
        anchor_capsule(empty, signer=None)  # type: ignore[arg-type]


def test_help_lists_new_flags() -> None:
    res = runner.invoke(evidence_app, ["attest-replay", "--help"])
    assert res.exit_code == 0
    assert "--certify" in res.output
    assert "--anchor" in res.output
