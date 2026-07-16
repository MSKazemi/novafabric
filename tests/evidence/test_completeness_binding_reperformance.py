"""ADR-0087 tests — completeness, criterion binding, re-performance."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.evidence import evidence_app
from novafabric.evidence.binding import (
    BINDING_PREDICATE_TYPE,
    CriterionBinding,
    EvidenceRef,
    attest_bindings,
    build_bindings,
    verify_binding,
)
from novafabric.evidence.completeness import (
    COMPLETENESS_PREDICATE_TYPE,
    attest_completeness,
    compute_completeness,
)
from novafabric.evidence.intoto import dsse_verify
from novafabric.evidence.reperformance import (
    REPERFORMANCE_PREDICATE_TYPE,
    build_reperformance_attestation,
    outcome_digest_of,
    sign_reperformance,
    verdict_for,
)
from novafabric.evidence.signing import LocalSigner, generate_keypair, verify_with_pem
from novafabric.replay._result import ReplayResult


def _capsule(tmp_path: Path, run_id: str = "run-ev-1") -> Path:
    capsule = tmp_path / run_id
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        "run_id: run-ev-1\ncapture:\n  level: full\n  hooks: [requests, mcp]\n"
        "  dropped_counts:\n    network_events: 2\n"
    )
    (capsule / "model-calls.jsonl").write_text(
        json.dumps({"started": "2026-06-12T08:00:00Z", "model": "m"})
        + "\n"
        + json.dumps({"started": "2026-06-12T08:05:00Z", "model": "m"})
        + "\n"
    )
    (capsule / "tool-calls.jsonl").write_text(
        json.dumps({"started": "2026-06-12T08:02:00Z", "tool_name": "db"}) + "\n"
    )
    (capsule / "lineage.jsonl").write_text(json.dumps({"edge_type": "consumed"}) + "\n")
    return capsule


@pytest.fixture()
def signer(tmp_path: Path) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path / "keys")
    return LocalSigner(priv)


class TestCompleteness:
    def test_counts_window_hooks_level(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        a = compute_completeness(capsule, run_id="run-ev-1")
        assert a.event_counts["model-calls.jsonl"] == 2
        assert a.event_counts["tool-calls.jsonl"] == 1
        assert a.capture_level == "full"
        assert a.active_hooks == ["requests", "mcp"]
        assert a.dropped_counts == {"network_events": 2}
        assert a.time_window is not None
        assert a.time_window.first == "2026-06-12T08:00:00Z"
        assert a.time_window.last == "2026-06-12T08:05:00Z"

    def test_empty_capsule_yields_empty_assertion(self, tmp_path: Path) -> None:
        capsule = tmp_path / "empty"
        capsule.mkdir()
        a = compute_completeness(capsule, run_id="empty")
        assert a.event_counts == {}
        assert a.time_window is None
        assert a.capture_level == "unknown"

    def test_attested_envelope_verifies(
        self, tmp_path: Path, signer: LocalSigner
    ) -> None:
        capsule = _capsule(tmp_path)
        a = compute_completeness(capsule, run_id="run-ev-1")
        envelope = attest_completeness(capsule, a, signer)

        def verify_fn(pae: bytes, sig: bytes) -> bool:
            return verify_with_pem(signer.public_pem, pae, sig)

        statement = dsse_verify(envelope, verify_fn)
        assert statement["predicateType"] == COMPLETENESS_PREDICATE_TYPE
        assert statement["predicate"]["event_counts"]["model-calls.jsonl"] == 2

    def test_tampered_envelope_rejected(
        self, tmp_path: Path, signer: LocalSigner
    ) -> None:
        import base64

        capsule = _capsule(tmp_path)
        a = compute_completeness(capsule, run_id="run-ev-1")
        envelope = attest_completeness(capsule, a, signer)
        tampered = json.loads(
            base64.b64decode(envelope["payload"]).decode()
        )
        tampered["predicate"]["event_counts"]["model-calls.jsonl"] = 999
        envelope["payload"] = base64.b64encode(
            json.dumps(tampered, sort_keys=True, separators=(",", ":")).encode()
        ).decode()

        def verify_fn(pae: bytes, sig: bytes) -> bool:
            return verify_with_pem(signer.public_pem, pae, sig)

        with pytest.raises(Exception):
            dsse_verify(envelope, verify_fn)


class TestCriterionBinding:
    def test_bindings_from_profile_statuses(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        bindings = build_bindings(capsule, "eu-ai-act-high-risk")
        assert bindings, "profile has controls"
        by_status = {b.binding_status for b in bindings}
        assert by_status <= {"satisfied", "partial", "unsatisfied"}
        satisfied = [b for b in bindings if b.binding_status == "satisfied"]
        assert all(b.evidence_refs for b in satisfied)
        for b in satisfied:
            for ref in b.evidence_refs:
                assert len(ref.sha256) == 64

    def test_verify_binding_detects_tamper(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        binding = CriterionBinding(
            criterion_id="X-1",
            evidence_refs=[
                EvidenceRef(
                    path="lineage.jsonl",
                    sha256=__import__("hashlib")
                    .sha256((capsule / "lineage.jsonl").read_bytes())
                    .hexdigest(),
                )
            ],
            binding_status="satisfied",
        )
        assert verify_binding(capsule, binding)
        (capsule / "lineage.jsonl").write_text("tampered\n")
        assert not verify_binding(capsule, binding)

    def test_attested_bindings_envelope(
        self, tmp_path: Path, signer: LocalSigner
    ) -> None:
        capsule = _capsule(tmp_path)
        bindings = build_bindings(capsule, "eu-ai-act-high-risk")
        envelope = attest_bindings(capsule, "run-ev-1", bindings, signer)

        def verify_fn(pae: bytes, sig: bytes) -> bool:
            return verify_with_pem(signer.public_pem, pae, sig)

        statement = dsse_verify(envelope, verify_fn)
        assert statement["predicateType"] == BINDING_PREDICATE_TYPE
        assert statement["predicate"]["bindings"]

    def test_unknown_profile_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Unknown profile"):
            build_bindings(_capsule(tmp_path), "no-such-profile")


def _result(status: str = "success", mode: str = "mocked") -> ReplayResult:
    return ReplayResult(
        replay_id="rp-1",
        replay_of_run_id="run-ev-1",
        mode=mode,
        status=status,
        start_time="2026-06-12T09:00:00Z",
        end_time="2026-06-12T09:00:05Z",
        duration_ms=5000,
        policy_flags_used=[],
        env_warnings=[],
    )


class TestReperformance:
    def test_outcome_digest_stable_across_wallclock(self) -> None:
        a, b = _result(), _result()
        b.start_time = "2027-01-01T00:00:00Z"
        b.duration_ms = 1
        assert outcome_digest_of(a) == outcome_digest_of(b)

    def test_verdicts(self) -> None:
        assert verdict_for(_result(mode="exact")) == "exact"
        assert verdict_for(_result(mode="mocked")) == "semantic-match"
        assert verdict_for(_result(status="failure")) == "mismatch"

    def test_signed_attestation_round_trip(
        self, tmp_path: Path, signer: LocalSigner
    ) -> None:
        capsule = _capsule(tmp_path)
        att = build_reperformance_attestation(capsule, _result())
        envelope = sign_reperformance(att, signer)

        def verify_fn(pae: bytes, sig: bytes) -> bool:
            return verify_with_pem(signer.public_pem, pae, sig)

        statement = dsse_verify(envelope, verify_fn)
        assert statement["predicateType"] == REPERFORMANCE_PREDICATE_TYPE
        assert statement["predicate"]["run_id"] == "run-ev-1"
        assert statement["predicate"]["match"] == "semantic-match"

    def test_unsigned_attestation_rejected_on_verify(
        self, tmp_path: Path, signer: LocalSigner
    ) -> None:
        capsule = _capsule(tmp_path)
        att = build_reperformance_attestation(capsule, _result())
        envelope = sign_reperformance(att, signer)
        envelope["signatures"] = []
        with pytest.raises(Exception):
            dsse_verify(envelope, lambda *a: False)


class TestCli:
    def test_completeness_unsigned_stdout(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        runner = CliRunner()
        result = runner.invoke(evidence_app, ["completeness", str(capsule)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["capture_level"] == "full"

    def test_completeness_signed_file(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        priv, _ = generate_keypair(tmp_path / "keys")
        out = tmp_path / "completeness.intoto.json"
        runner = CliRunner()
        result = runner.invoke(
            evidence_app,
            ["completeness", str(capsule), "--key", str(priv), "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        envelope = json.loads(out.read_text())
        assert envelope["signatures"]

    def test_completeness_output_creates_parent_dirs(self, tmp_path: Path) -> None:
        """-o some/new/dir/file.json must create the directories, not crash."""
        capsule = _capsule(tmp_path)
        priv, _ = generate_keypair(tmp_path / "keys")
        out = tmp_path / "does" / "not" / "exist" / "completeness.intoto.json"
        runner = CliRunner()
        result = runner.invoke(
            evidence_app,
            ["completeness", str(capsule), "--key", str(priv), "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(out.read_text())["signatures"]

    def test_bind_writes_bindings_file(self, tmp_path: Path) -> None:
        capsule = _capsule(tmp_path)
        out = tmp_path / "bindings.json"
        runner = CliRunner()
        result = runner.invoke(
            evidence_app,
            [
                "bind",
                str(capsule),
                "--profile",
                "eu-ai-act-high-risk",
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(out.read_text())["bindings"]

    def test_invalid_capsule_exits_nonzero(self) -> None:
        runner = CliRunner()
        result = runner.invoke(evidence_app, ["completeness", "no-such-run"])
        assert result.exit_code == 1
