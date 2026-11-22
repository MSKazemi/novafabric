"""EvidenceLab D2: judges for the two-variant walkthrough — pure functions, no process IO.

``run.py`` runs the real commands (``nova register``, ``nova capture`` twice, ``nova diff``,
``nova lineage provenance``, ``nova verify``, ``nova replay --mode forensic``) and hands
their *parsed output* to the functions here. Each function returns one :class:`Check`:
a verdict on one axis, with the expected and observed values, the method used and the
artifact it read. Nothing here decides a verdict from NovaFabric's own summary alone —
the expected side always comes from the scenario fixtures or from a constant of this
example.

Verdicts are ``PASS | FAIL | NOT_EVALUATED | UNSUPPORTED``. A check also carries the
verdict this scenario *expects*: the unsafe variant's task is expected to ``FAIL`` and
an unsealed capsule's integrity is expected to be ``NOT_EVALUATED``. The harness verdict
is ``PASS`` only when every check's verdict equals its expected verdict.

The report built here is a local diagnostic sidecar for this example. It is not a
NovaFabric portable format, it is not signed, and it is not an Evidence Bundle.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Verdict = Literal["PASS", "FAIL", "NOT_EVALUATED", "UNSUPPORTED"]
VERDICTS: tuple[Verdict, ...] = ("PASS", "FAIL", "NOT_EVALUATED", "UNSUPPORTED")
AXES = ("task", "capture", "integrity", "replay")
PROFILES = ("zero-key", "signed-offline")

#: The synthetic input dataset registered in the walkthrough's fresh, temporary registry.
ASSET_NAME = "evidencelab-service-incident"
ASSET_VERSION = "1.0.0"
ASSET_REF = f"{ASSET_NAME}@{ASSET_VERSION}"
#: ``nova lineage provenance`` names a bare ``name@version`` in the ``local`` registry.
ASSET_NODE_REF = f"local:{ASSET_REF}"


@dataclass(frozen=True)
class Check:
    axis: str
    variant: str | None
    name: str
    verdict: Verdict
    reason_code: str
    expected: Any
    observed: Any
    oracle_method: str
    artifact_reference: str | None
    expected_verdict: Verdict
    ran: bool = True
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.axis not in AXES:
            raise ValueError(f"unknown axis {self.axis!r}")
        if self.verdict not in VERDICTS or self.expected_verdict not in VERDICTS:
            raise ValueError("verdicts must be PASS|FAIL|NOT_EVALUATED|UNSUPPORTED")

    @property
    def as_expected(self) -> bool:
        return self.verdict == self.expected_verdict

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["as_expected"] = self.as_expected
        return out


# ── task ─────────────────────────────────────────────────────────────────────


def judge_task(variant: str, evaluated_task: dict[str, Any], *, expected: Verdict,
               capsule_ref: str | None) -> Check:  # fmt: skip
    """The policy oracle's verdict on the agent's decision (computed by the D1 judge)."""
    verdict = evaluated_task.get("verdict")
    if verdict not in ("PASS", "FAIL"):
        return Check("task", variant, "policy_oracle_verdict", "FAIL", "task-verdict-missing",
                     expected, verdict, "fixed policy oracle over the recorded decision",
                     capsule_ref, expected)  # fmt: skip
    return Check(
        "task", variant, "policy_oracle_verdict", verdict,
        "policy-accepted" if verdict == "PASS" else "policy-rejected",
        expected, {"verdict": verdict, "reason": evaluated_task.get("reason")},
        "fixed policy oracle over the recorded decision (never the model's explanation)",
        capsule_ref, expected,
    )  # fmt: skip


# ── capture ──────────────────────────────────────────────────────────────────


def judge_capture(variant: str, observed: dict[str, Any], d1_checks: dict[str, bool], *,
                  expected_decision_sha256: str, recorded_decision_sha256: str | None,
                  capsule_ref: str | None) -> Check:  # fmt: skip
    """One logical model call, two read-only tool calls, digests, decision digest."""
    want = {
        "logical_model_calls": 1,
        "tool_calls": 2,
        "tool_sequence": ["read_metrics", "search_logs"],
        "decision_sha256": expected_decision_sha256,
    }
    got = {
        "logical_model_calls": observed.get("logical_model_calls"),
        "tool_calls": observed.get("tool_calls"),
        "tool_sequence": observed.get("tool_sequence"),
        "decision_sha256": recorded_decision_sha256,
    }
    failed = sorted(k for k, ok in d1_checks.items() if ok is not True)
    if got != want:
        reason = "decision-digest-drift" if got["decision_sha256"] != want["decision_sha256"] \
            else "capture-count-mismatch"  # fmt: skip
        verdict: Verdict = "FAIL"
    elif failed or not d1_checks:
        reason, verdict = "capture-check-failed", "FAIL"
    else:
        reason, verdict = "capture-matches-fixtures", "PASS"
    return Check(
        "capture", variant, "logical_calls_and_decision", verdict, reason, want,
        {**got, "failed_checks": failed},
        "capsule streams vs counts and a decision digest computed from the fixture reply",
        capsule_ref, "PASS",
    )  # fmt: skip


def judge_capture_aborted(variant: str, code: str, message: str) -> Check:
    """A variant whose capture or D1 check failed is a named capture FAIL, not a gap.

    The run stops there (later checks would read a capsule that is not trustworthy), but the
    report still names which variant failed and with which ``E_*`` code.
    """
    return Check("capture", variant, "logical_calls_and_decision", "FAIL", code,
                 "every D1 capture check holds", message,
                 "the D1 capture judge (counts, order, digests, privacy, golden transcript)",
                 None, "PASS")  # fmt: skip


# ── diff ─────────────────────────────────────────────────────────────────────


def judge_diff(diff: Any, *, run_a: str, run_b: str, call_a: str | None, call_b: str | None,
               exit_code: int, artifact: str | None) -> Check:  # fmt: skip
    """``nova diff`` must name the changed model call and leave the tool calls identical.

    Expected (from the scenario, not from the diff): both variants read the same
    observations, so both tool calls align unchanged; the prompt differs, so the one
    model call is changed — its request (the corrective prompt) and its response (the
    decision) — while provider and parameters stay the same; nothing is added or removed.
    The changed pair must be exactly the two logical model records the capture judge read
    from each capsule (``call_a`` / ``call_b``), so a diff that paired the wrong records
    cannot pass on matching flags alone.
    """
    want = {
        "run_a_id": run_a, "run_b_id": run_b, "has_changes": True,
        # measured: nova diff counts a changed pair as aligned too (aligned 1, changed 1)
        "model_calls": {"aligned": 1, "changed": 1, "added": 0, "removed": 0},
        "tool_calls": {"aligned": 2, "changed": 0, "added": 0, "removed": 0},
        "model_pairs": [{"changed": True, "request_changed": True, "response_changed": True,
                         "provider_changed": False, "params_changed": False,
                         "model_call_id_a": call_a, "model_call_id_b": call_b}],
    }  # fmt: skip
    pair_keys = ("changed", "request_changed", "response_changed", "provider_changed",
                 "params_changed", "model_call_id_a", "model_call_id_b")  # fmt: skip
    if not call_a or not call_b:
        return Check("capture", None, "nova_diff_names_the_change", "FAIL", "diff-no-call-ids",
                     want, {"call_a": call_a, "call_b": call_b}, "capsule logical records",
                     artifact, "PASS")  # fmt: skip
    method = "nova diff --output-format json vs the expected change set of the two variants"
    if exit_code != 0 or not isinstance(diff, dict):
        return Check("capture", None, "nova_diff_names_the_change", "FAIL", "diff-did-not-run",
                     want, {"exit_code": exit_code}, method, artifact, "PASS")  # fmt: skip
    raw_sections = diff.get("sections")
    sections: dict[str, Any] = raw_sections if isinstance(raw_sections, dict) else {}
    model = sections.get("model_calls") or {}
    tools = sections.get("tool_calls") or {}
    got = {
        "run_a_id": diff.get("run_a_id"), "run_b_id": diff.get("run_b_id"),
        "has_changes": diff.get("has_changes"),
        "model_calls": {k: model.get(k) for k in ("aligned", "changed", "added", "removed")},
        "tool_calls": {k: tools.get(k) for k in ("aligned", "changed", "added", "removed")},
        "model_pairs": [
            {k: p.get(k) for k in pair_keys}
            for p in (model.get("pairs") or [])
            if isinstance(p, dict) and p.get("changed")
        ],
    }  # fmt: skip
    if got != want:
        return Check("capture", None, "nova_diff_names_the_change", "FAIL", "diff-mismatch",
                     want, got, method, artifact, "PASS")  # fmt: skip
    return Check("capture", None, "nova_diff_names_the_change", "PASS", "diff-matches-variants",
                 want, got, method, artifact, "PASS")  # fmt: skip


# ── lineage ──────────────────────────────────────────────────────────────────


def judge_lineage(variant: str, provenance: Any, edges: list[dict[str, Any]], *, run_id: str,
                  exit_code: int, artifact: str | None) -> Check:  # fmt: skip
    """The run's provenance must be exactly the registered dataset — never empty.

    Two independent views must agree: the capsule's own ``lineage.jsonl`` (a ``consumed``
    edge from this run to the asset) and the indexed store's ``nova lineage provenance``.
    An empty answer is a FAIL, not a quiet success.
    """
    want = {"provenance_refs": [ASSET_NODE_REF], "consumed_edges": [[run_id, ASSET_REF]]}
    method = "nova lineage provenance <run> --output json + capsule lineage.jsonl vs the asset"
    if exit_code != 0 or not isinstance(provenance, list):
        return Check("capture", variant, "run_consumed_registered_asset", "FAIL",
                     "lineage-query-failed", want, {"exit_code": exit_code}, method, artifact,
                     "PASS")  # fmt: skip
    refs = sorted(str(n.get("ref")) for n in provenance if isinstance(n, dict))
    consumed = [
        [str((e.get("source") or {}).get("run_id")), str((e.get("target") or {}).get("asset_ref"))]
        for e in edges
        if isinstance(e, dict) and e.get("edge_type") == "consumed"
    ]
    got = {"provenance_refs": refs, "consumed_edges": consumed}
    if not refs:
        reason = "lineage-empty"
    elif refs != want["provenance_refs"]:
        reason = "lineage-unexpected-asset"
    elif consumed != want["consumed_edges"]:
        reason = "lineage-edge-mismatch"
    else:
        return Check("capture", variant, "run_consumed_registered_asset", "PASS",
                     "lineage-matches-registered-asset", want, got, method, artifact,
                     "PASS")  # fmt: skip
    return Check("capture", variant, "run_consumed_registered_asset", "FAIL", reason, want, got,
                 method, artifact, "PASS")  # fmt: skip


# ── integrity ────────────────────────────────────────────────────────────────

_VERIFY_FIELDS = (
    "valid", "sealed", "signature_ok", "capsule_binding_ok", "ca_chain_checked", "ca_chain_ok",
    "identity_trust", "timestamp_trust", "log_inclusion", "signer_subject", "error",
)  # fmt: skip


def _verify_summary(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"parsed": False}
    return {k: result.get(k) for k in _VERIFY_FIELDS}


def judge_integrity(variant: str, profile: str, verify: Any, *, exit_code: int,
                    artifact: str | None) -> Check:  # fmt: skip
    """The native ``nova verify --json`` result, read field by field.

    zero-key: no signer is configured, so the verifier must say ``sealed: false`` and the
    axis is NOT_EVALUATED (``seal-not-configured``). ``nova validate`` passing elsewhere
    never turns this into a PASS. signed-offline: PASS only when the verifier exits 0 and
    reports a valid signature, an intact capsule binding and a chain to the pinned local CA.
    The identity is self-asserted; a pinned local CA is continuity, not external identity.
    """
    got = {"exit_code": exit_code, **_verify_summary(verify)}
    method = "nova verify --json (native verifier)" + (
        " --ca-bundle <the disposable local seal CA>" if profile == "signed-offline" else ""
    )
    if profile == "zero-key":
        want: dict[str, Any] = {"sealed": False}
        if isinstance(verify, dict) and verify.get("sealed") is False:
            return Check(
                "integrity", variant, "native_seal_verification", "NOT_EVALUATED",
                "seal-not-configured", want, got, method, artifact, "NOT_EVALUATED",
                notes=["nova validate passing is structural only; it is not a seal"],
            )  # fmt: skip
        return Check(
            "integrity", variant, "native_seal_verification", "FAIL", "unexpected-seal-state",
            want, got, method, artifact, "NOT_EVALUATED",
        )  # fmt: skip
    want = {
        "exit_code": 0, "valid": True, "sealed": True, "signature_ok": True,
        "capsule_binding_ok": True, "ca_chain_ok": True, "identity_trust": "local-ca-pinned",
    }  # fmt: skip
    if not isinstance(verify, dict) or verify.get("sealed") is not True:
        return Check("integrity", variant, "native_seal_verification", "FAIL", "seal-missing",
                     want, got, method, artifact, "PASS")  # fmt: skip
    ok = exit_code == 0 and all(verify.get(k) == v for k, v in want.items() if k != "exit_code")
    return Check(
        "integrity", variant, "native_seal_verification", "PASS" if ok else "FAIL",
        "verified-against-pinned-local-ca" if ok else "verification-failed", want, got, method,
        artifact, "PASS",
        notes=["identity is self-asserted (nova seal init); a pinned local CA proves continuity "
               "with this disposable installation, not who the signer is",
               "no timestamp authority was used (timestamp_trust reported as observed)"],
    )  # fmt: skip


#: The verifier field that must name *why* each broken copy is refused. A refusal for some
#: other reason (say, the copy itself is unreadable) does not prove the case.
NEGATIVE_SIGNALS: dict[str, dict[str, Any]] = {
    "tampered-member": {"capsule_binding_ok": False},
    "missing-member": {"capsule_binding_ok": False},
    "corrupt-signature": {"signature_ok": False},
    "wrong-ca": {"ca_chain_ok": False},
}


def judge_negative_proof(case: str, verify: Any, *, exit_code: int,
                         artifact: str | None) -> Check:  # fmt: skip
    """A deliberately broken copy of a sealed capsule must NOT verify — for the right reason."""
    got = {"exit_code": exit_code, **_verify_summary(verify)}
    signal = NEGATIVE_SIGNALS[case]
    want = {"exit_code": "nonzero", "valid": False, **signal}
    method = "nova verify --json on a deliberately broken copy (expected to fail)"
    refused = exit_code != 0 and isinstance(verify, dict) and verify.get("valid") is False
    if not refused:
        verdict: Verdict = "PASS"
        reason = "false-green"
    elif all(verify.get(k) == v for k, v in signal.items()):
        verdict, reason = "FAIL", "verifier-refused"
    else:
        verdict, reason = "NOT_EVALUATED", "refused-for-another-reason"
    return Check("integrity", f"negative:{case}", "broken_copy_is_refused", verdict, reason,
                 want, got, method, artifact, "FAIL")  # fmt: skip


def judge_pristine_copy(verify: Any, *, exit_code: int, artifact: str | None) -> Check:
    """Positive control: an UNTOUCHED copy made the same way must still verify.

    Without it, a copy step that broke every copy would make all negative proofs pass.
    """
    inner = judge_integrity("control:pristine-copy", "signed-offline", verify,
                            exit_code=exit_code, artifact=artifact)  # fmt: skip
    return Check("integrity", "control:pristine-copy", "untouched_copy_verifies", inner.verdict,
                 inner.reason_code, inner.expected, inner.observed,
                 "nova verify --json on an unmodified copy made like the broken ones", artifact,
                 "PASS")  # fmt: skip


def _is_proof_case(variant: str | None) -> bool:
    return (variant or "").startswith(("negative:", "control:"))


# ── replay ───────────────────────────────────────────────────────────────────


def judge_replay(variant: str, *, exit_code: int, replay_id: str | None,
                 artifact: str | None, artifact_exists: bool) -> Check:  # fmt: skip
    """Forensic replay is read-only inspection; it says nothing about replay fidelity."""
    ran = exit_code == 0 and bool(replay_id) and artifact_exists
    got = {"forensic_inspection_ran": ran, "exit_code": exit_code, "replay_id": replay_id,
           "replay_output_exists": artifact_exists}  # fmt: skip
    want = {"forensic_inspection_exit_code": 0, "replay_output_exists": True}
    method = "nova replay --mode forensic (read-only; no re-execution)"
    if not ran:
        return Check("replay", variant, "replay_fidelity", "FAIL", "forensic-inspection-failed",
                     want, got, method, artifact, "NOT_EVALUATED")  # fmt: skip
    return Check(
        "replay", variant, "replay_fidelity", "NOT_EVALUATED", "forensic-only", want, got, method,
        artifact, "NOT_EVALUATED",
        notes=["forensic inspection ran and exited 0; no mocked or exact replay was run, so no "
               "fidelity claim is made"],
    )  # fmt: skip


# ── summary ──────────────────────────────────────────────────────────────────


def axis_summary(checks: list[Check]) -> dict[str, Any]:
    """Per axis and variant: the observed verdict (never collapsed into one badge)."""
    out: dict[str, Any] = {}
    for c in checks:
        if c.axis not in AXES or _is_proof_case(c.variant):
            continue
        out.setdefault(c.axis, {}).setdefault(c.variant or "both", {})[c.name] = {
            "verdict": c.verdict,
            "reason_code": c.reason_code,
            "expected_verdict": c.expected_verdict,
        }
    return out


def harness_verdict(checks: list[Check]) -> Verdict:
    return "PASS" if checks and all(c.as_expected for c in checks) else "FAIL"


def proof_requirement_met(checks: list[Check]) -> bool:
    """``--require-verified``: every variant's integrity check must be a PASS."""
    integrity = [c for c in checks if c.axis == "integrity" and not _is_proof_case(c.variant)]
    return bool(integrity) and all(c.verdict == "PASS" for c in integrity)
