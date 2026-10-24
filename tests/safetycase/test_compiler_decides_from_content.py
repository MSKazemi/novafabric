"""The safety-case compiler decides each backing state from content (review misc-1-01/02).

Its claim is that backing states are assigned mechanically from the evidence. They
were assigned from the *existence* of files instead, and one statistical rule was
missing:

* an eval whose Wilson interval lay wholly **below** the pass threshold (10/100,
  consensus "fail") was SUPPORTED — only a straddling interval was caught;
* a ``completeness.json`` reporting dropped events was SUPPORTED;
* any ``seal.json`` that parsed was a SUPPORTED seal, while a real ``.seal/`` was
  never looked at;
* an unsigned, hand-written ``reperformance.json`` was SUPPORTED and deductive.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.evidence.reperformance import (
    ReperformanceAttestation,
    sign_reperformance,
)
from novafabric.evidence.signing import LocalSigner, generate_keypair
from novafabric.safetycase.compiler import DEFAULT_PASS_THRESHOLD, SafetyCaseCompiler
from novafabric.safetycase.models import BackingState, Evidence

RUN = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / RUN
    cap.mkdir()
    (cap / "capsule.yaml").write_text(f"run_id: {RUN}\n")
    return cap


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _resolve(cap: Path, kind: str, **compiler: Any):  # type: ignore[no-untyped-def]
    c = SafetyCaseCompiler(**compiler)
    return c._RESOLVERS[kind](c, cap)


# ------------------------------------------------------------------- eval


def _eval(cap: Path, successes: int, n: int, verdict: str = "pass", kappa: float = 0.9) -> None:
    _write(cap / "eval-results" / "e.json", {
        "judges": ["a", "b"], "inter_judge_agreement": kappa, "consensus_verdict": verdict,
        "sample_size": n, "successes": successes,
    })


@pytest.mark.parametrize(
    ("successes", "state"),
    [
        (10, BackingState.UNSUPPORTED),   # CI wholly below the threshold
        (70, BackingState.CONTESTED),     # CI straddles it
        (95, BackingState.SUPPORTED),     # CI wholly above it
    ],
    ids=["below", "straddling", "above"],
)
def test_eval_state_follows_the_interval(
    tmp_path: Path, successes: int, state: BackingState
) -> None:
    cap = _capsule(tmp_path)
    _eval(cap, successes, 100)
    assert _resolve(cap, "judge_aggregate").state is state


def test_a_failing_consensus_verdict_is_never_supported(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _eval(cap, 95, 100, verdict="fail")
    assert _resolve(cap, "judge_aggregate").state is BackingState.UNSUPPORTED


def test_an_eval_with_no_interval_is_unknown(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write(cap / "eval-results" / "e.json", {"judges": ["a"], "consensus_verdict": "pass"})
    assert _resolve(cap, "judge_aggregate").state is BackingState.UNKNOWN


def test_the_threshold_itself_is_the_boundary(tmp_path: Path) -> None:
    assert 0 < DEFAULT_PASS_THRESHOLD < 1


# ------------------------------------------------------------ completeness


def test_completeness_with_dropped_events_is_contested(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write(cap / "completeness.json", {
        "run_id": RUN, "event_counts": {"model-calls": 3}, "dropped_counts": {"model-calls": 37},
        "generated_at": "2026-10-24T00:00:00Z",
    })
    resolved = _resolve(cap, "completeness_assertion")
    assert resolved.state is BackingState.CONTESTED
    assert "37" in (resolved.contest_reason or "")


def test_complete_capture_is_supported(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write(cap / "completeness.json", {
        "run_id": RUN, "event_counts": {"model-calls": 3}, "dropped_counts": {"model-calls": 0},
        "generated_at": "2026-10-24T00:00:00Z",
    })
    assert _resolve(cap, "completeness_assertion").state is BackingState.SUPPORTED


def test_a_completeness_file_that_is_not_one_is_unknown(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write(cap / "completeness.json", {"anything": True})
    assert _resolve(cap, "completeness_assertion").state is BackingState.UNKNOWN


# ------------------------------------------------------------------- seal


def test_a_seal_json_file_is_not_a_seal(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write(cap / "seal.json", {})
    _write(cap / "seal-bundle.json", {"sig": "x"})
    assert _resolve(cap, "seal") is None  # no seal evidence at all


def test_a_seal_that_does_not_verify_is_contested(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / ".seal").mkdir()
    (cap / ".seal" / "manifest.dsse").write_text('{"payload": "", "signatures": []}')
    resolved = _resolve(cap, "seal")
    assert resolved is not None
    assert resolved.state is BackingState.CONTESTED


# ----------------------------------------------------------------- replay


def _signed_replay(cap: Path, key_dir: Path, capsule_hash: str | None = None) -> Path:
    priv, pub = generate_keypair(key_dir)
    attestation = ReperformanceAttestation(
        run_id=RUN,
        capsule_hash=capsule_hash
        or hashlib.sha256((cap / "capsule.yaml").read_bytes()).hexdigest(),
        replayed_at="2026-10-24T00:00:00Z",
        replay_mode="exact",
        outcome_digest="d" * 64,
        match="exact",
        replayer={"name": "novafabric", "version": "test"},
    )
    _write(cap / "attestations" / "reperformance.json",
           sign_reperformance(attestation, LocalSigner(priv)))
    return pub


def test_an_unsigned_replay_attestation_is_unknown(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _write(cap / "attestations" / "reperformance.json", {"match": "exact"})
    assert _resolve(cap, "replay_attestation").state is BackingState.UNKNOWN


def test_a_signed_replay_without_a_trusted_key_is_unknown(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _signed_replay(cap, tmp_path / "k")
    assert _resolve(cap, "replay_attestation").state is BackingState.UNKNOWN


def test_a_replay_signed_by_the_trusted_key_is_supported(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    pub = _signed_replay(cap, tmp_path / "k")
    resolved = _resolve(cap, "replay_attestation", replay_public_keys=[pub.read_bytes()])
    assert resolved.state is BackingState.SUPPORTED


def test_a_replay_signed_by_another_key_is_unknown(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    _signed_replay(cap, tmp_path / "k")
    _, other = generate_keypair(tmp_path / "other")
    resolved = _resolve(cap, "replay_attestation", replay_public_keys=[other.read_bytes()])
    assert resolved.state is BackingState.UNKNOWN


def test_a_replay_of_another_capsule_state_is_contested(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    pub = _signed_replay(cap, tmp_path / "k", capsule_hash="0" * 64)
    resolved = _resolve(cap, "replay_attestation", replay_public_keys=[pub.read_bytes()])
    assert resolved.state is BackingState.CONTESTED


def test_the_seal_evidence_points_at_the_real_seal(tmp_path: Path) -> None:
    """The artifact a seal leaf cites is ``.seal/manifest.dsse``, which the verifier re-hashes."""
    cap = _capsule(tmp_path)
    (cap / ".seal").mkdir()
    (cap / ".seal" / "manifest.dsse").write_text("{}")
    resolved = _resolve(cap, "seal")
    assert isinstance(resolved.evidence, Evidence)
    assert resolved.evidence.artifact_ref.path_in_bundle == ".seal/manifest.dsse"
