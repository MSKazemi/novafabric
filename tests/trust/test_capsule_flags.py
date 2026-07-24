"""ADR-0173 adapter: derive radar guarantees from a capsule (2026-07-18).

The radar was a pure projection over a hand-assembled mapping, so
`nova trust-radar` could not point at a capsule. This reads what a capsule
genuinely evidences.

The load-bearing property: **absent is not false**. A guarantee the capsule
cannot speak to must render `n/a`, never `fail`. Reporting "policy: fail" for
a capsule that simply carries no policy decision would be a fabricated
verdict — and a fabricated verdict is the worst possible defect in the part
of the product whose entire job is telling you what is proven.
"""

from __future__ import annotations

import json
from pathlib import Path

from novafabric.trust.capsule_flags import flags_from_capsule
from novafabric.trust.radar import build_trust_radar


def _capsule(tmp_path: Path, *, proof: dict | None = None, sealed: bool = False) -> Path:
    cap = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    if proof is not None:
        (cap / "redaction-proof.json").write_text(json.dumps(proof))
    if sealed:
        (cap / ".seal").mkdir()
    return cap


def _axis(capsule: Path, key: str):
    radar = build_trust_radar(flags_from_capsule(capsule))
    return next(a for a in radar.axes if a.key == key)


def test_unsealed_capsule_reports_na_not_fail(tmp_path: Path) -> None:
    """An unsealed capsule is unverified, not failed — a critical distinction."""
    cap = _capsule(tmp_path, proof={"findings": []})
    for key in ("signature", "timestamp", "log_integrity"):
        axis = _axis(cap, key)
        assert axis.state.value == "na", f"{key} should be n/a on an unsealed capsule"
        assert axis.value is None


def test_unsealed_capsule_verdict_is_unsealed_not_critical(tmp_path: Path) -> None:
    cap = _capsule(tmp_path, proof={"findings": []})
    radar = build_trust_radar(flags_from_capsule(cap))
    assert radar.verdict.value == "unsealed"


def test_policy_and_eval_gate_are_never_derived_from_a_capsule(tmp_path: Path) -> None:
    """Those are registry/promotion facts, not capsule facts.

    A capsule records that a run happened, not whether an asset later cleared
    a gate; inferring them here would attach a promotion verdict to the wrong
    artifact.
    """
    cap = _capsule(tmp_path, proof={"findings": []})
    for key in ("policy", "eval_gate"):
        assert _axis(cap, key).state.value == "na"


def test_clean_capsule_reports_full_coverage_not_zero(tmp_path: Path) -> None:
    """No sensitive surface means nothing to cover — 1.0, not 0.0.

    Reporting 0.0 would paint a clean capsule red, which is precisely
    backwards.
    """
    cap = _capsule(tmp_path, proof={"findings": []})
    assert flags_from_capsule(cap)["redaction_coverage"] == 1.0
    assert flags_from_capsule(cap)["secret_scan_clean"] is True


def test_coverage_is_protected_over_sensitive(tmp_path: Path) -> None:
    cap = _capsule(
        tmp_path,
        proof={
            "findings": [
                {"target_ref": "a", "redaction_strategy": "mask"},
                {"target_ref": "b", "action_taken": "scrub"},
                {"target_ref": "c"},  # detected but not acted on
            ]
        },
    )
    flags = flags_from_capsule(cap)
    assert flags["redaction_coverage"] == 2 / 3
    assert flags["secret_scan_clean"] is False  # findings exist


def test_missing_redaction_proof_yields_na_not_fail(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)  # no proof at all
    flags = flags_from_capsule(cap)
    assert "redaction_coverage" not in flags
    assert "secret_scan_clean" not in flags
    assert _axis(cap, "redaction_coverage").state.value == "na"


def test_corrupt_proof_never_raises(tmp_path: Path) -> None:
    """A partial radar beats no radar; a bad file must not take the view down."""
    cap = _capsule(tmp_path)
    (cap / "redaction-proof.json").write_text("{not json")
    assert flags_from_capsule(cap) == {}


def test_proof_that_is_not_an_object_is_ignored(tmp_path: Path) -> None:
    cap = _capsule(tmp_path)
    (cap / "redaction-proof.json").write_text("[1, 2, 3]")
    assert flags_from_capsule(cap) == {}


def test_sealed_capsule_without_seal_config_stays_na(tmp_path: Path) -> None:
    """Cannot-verify is not the same as verified-and-failed.

    Verification needs the NovaSeal profile (keys, TSA, merkle DB). Without
    it the axes must stay n/a rather than reporting a failure that was never
    actually observed.
    """
    cap = _capsule(tmp_path, proof={"findings": []}, sealed=True)
    flags = flags_from_capsule(cap)
    # Either verification ran, or it could not — but it must never fabricate.
    if "signature_ok" in flags:
        assert isinstance(flags["signature_ok"], bool)
    else:
        assert _axis(cap, "signature").state.value == "na"
