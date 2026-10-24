"""Derive trust-radar guarantees from a capsule on disk (ADR-0173 adapter).

The radar model is a pure projection over a mapping of seven guarantees; until
now that mapping had to be hand-assembled, so `nova trust-radar` could not
point at a capsule. This reads what a capsule genuinely knows.

**Absent is not false.** The radar already distinguishes a *failed* guarantee
from an *unavailable* one — an absent key renders as an ``n/a`` axis. So this
adapter populates only what the capsule actually evidences and leaves the rest
absent, rather than defaulting to ``False``. Reporting "policy: fail" for a
capsule that simply carries no policy decision would be a fabricated verdict,
and this is the one part of the product where a fabricated verdict is worst.

Derivable from a capsule today:

- ``signature_ok`` / ``timestamp_ok`` / ``log_integrity_ok`` — the same
  verification ``nova verify <capsule-dir>`` runs (``verify_capsule``). The
  signature axis passes only when the signature verifies *and* binds this
  directory (ADR-0251: ``capsule.yaml`` equals the signed payload, every
  ``evidence_digests`` entry matches, the log entry names this payload) and any
  configured signer chain validates. The timestamp axis is
  *trusted time*, not token integrity: ``True`` only for an anchored token
  (``tsa_ca_certs``), ``0.5`` (warn) for an intact but unanchored one — a
  self-made TSA reaches that state — ``False`` for an invalid one, absent for none.
- ``redaction_coverage`` / ``secret_scan_clean`` — the capsule's
  ``redaction-proof.json``.

Deliberately left absent (→ ``n/a``): ``policy_pass`` and ``eval_gate_pass``.
Those are *registry/promotion* facts, not capsule facts — a capsule records
that a run happened, not whether an asset later cleared a gate. Inferring them
here would attach a promotion verdict to the wrong artifact.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from novafabric._capsule_fs import CapsuleRequiredFileError, read_required_capsule_member

log = logging.getLogger(__name__)


def _seal_flags(capsule_dir: Path) -> dict[str, Any]:
    """NovaSeal verification flags, or ``{}`` when the capsule is unsealed.

    Runs :func:`~novafabric.trust.novaseal.capsule_verify.verify_capsule`, the
    function ``nova verify <capsule-dir>`` reports from, so the radar can never
    call a capsule attested that ``nova verify`` rejects (audit H1). The
    Signature axis is the signature *bound to this directory* (ADR-0251): a
    capsule edited after sealing, or carrying another capsule's ``.seal/``, fails
    it even though the signature itself verifies.
    """
    seal_dir = capsule_dir / ".seal"
    if not seal_dir.is_dir():
        return {}  # unsealed → the three seal axes stay n/a, not fail

    # Verification is self-contained (ADR-0297 S2), as in `nova verify`: the
    # profile only adds the sealer's log and operator anchors. A config that
    # does not load means verification could not run — n/a, not fail.
    try:
        from novafabric.trust.novaseal.capsule_verify import verify_capsule  # noqa: PLC0415
        from novafabric.trust.novaseal.config import (  # noqa: PLC0415
            load_signing_profile,
        )

        profile = load_signing_profile()
    except Exception:  # noqa: BLE001 — a verification fault must not crash the view
        log.warning("seal verification unavailable for %s", capsule_dir, exc_info=True)
        return {}
    try:
        report = verify_capsule(capsule_dir, profile)
    except Exception:  # noqa: BLE001 — a verification fault must not crash the view
        # A seal is present and its verification broke: that is not "unsealed",
        # which is what an empty result renders as (review trust-evidence-03).
        log.warning("seal verification failed with an error for %s", capsule_dir, exc_info=True)
        return {"signature_ok": False, "valid": False}

    result = report.result
    flags: dict[str, Any] = {
        "signature_ok": report.seal_binds_capsule,
        # ADR-0301: no token is "n/a" (None), not a passed timestamp axis. An
        # intact token whose TSA no anchor vouches for is not trusted time: warn.
        "timestamp_ok": _timestamp_axis(result.timestamp_trust),
        "log_integrity_ok": _log_axis(result.log_inclusion),
        # Additive, not radar axes: what `nova verify --json` reports.
        "capsule_binding_ok": report.binding.ok and report.capsule_id_ok,
        "valid": report.valid,
    }
    if not report.valid and _no_axis_fails(flags):
        # Defence in depth: whatever made `nova verify` fail must show on the
        # radar. Every known cause maps to an axis above; an unmapped one fails
        # the Signature axis rather than leaving the radar attested.
        flags["signature_ok"] = False
    return flags


def _no_axis_fails(flags: dict[str, Any]) -> bool:
    return all(flags[k] is not False for k in ("signature_ok", "timestamp_ok", "log_integrity_ok"))


def _log_axis(log_inclusion: str) -> bool | float:
    """Radar value of the log-integrity axis for a ``log_inclusion`` value.

    An entry no available log or carried proof could check (``not-checked``)
    does not invalidate the seal and ``nova verify`` accepts it, so it warns; it
    read ``critical``, the verdict of a forged entry (review trust-evidence-14).
    """
    from novafabric.trust.novaseal import (  # noqa: PLC0415
        LOG_INCLUSION_LOCAL,
        LOG_INCLUSION_NOT_CHECKED,
        LOG_INCLUSION_PROOF,
    )

    if log_inclusion in (LOG_INCLUSION_LOCAL, LOG_INCLUSION_PROOF):
        return True
    return 0.5 if log_inclusion == LOG_INCLUSION_NOT_CHECKED else False


def _timestamp_axis(timestamp_trust: str) -> bool | float | None:
    """Radar value of the timestamp axis for a ``timestamp_trust`` value."""
    return {"anchored": True, "unanchored": 0.5, "invalid": False}.get(timestamp_trust)


def _redaction_flags(capsule_dir: Path) -> dict[str, Any]:
    """Redaction coverage + secret-scan cleanliness from ``redaction-proof.json``."""
    proof_path = capsule_dir / "redaction-proof.json"
    try:
        capsule_text = read_required_capsule_member(proof_path, operation="capsule flags")
    except CapsuleRequiredFileError:
        # Present but unusable (a link, FIFO, directory): that is not "captured
        # without the masking pipeline". Both axes fail; never n/a, never clean.
        log.warning("redaction proof at %s is present but unusable", proof_path)
        return {"secret_scan_clean": False, "redaction_coverage": 0.0}
    if capsule_text is None:
        return {}  # captured without the masking pipeline → n/a, not fail

    try:
        proof = json.loads(capsule_text)
    except ValueError:
        log.warning("unreadable redaction proof at %s", proof_path)
        return {}
    if not isinstance(proof, dict):
        return {}

    findings = proof.get("findings") or []
    flags: dict[str, Any] = {
        # No findings means nothing sensitive was detected — a clean scan.
        "secret_scan_clean": len(findings) == 0,
    }

    # Coverage = protected ÷ sensitive. With no sensitive surface there is
    # nothing to cover: report 1.0 (fully covered) rather than 0.0, which
    # would read as "nothing protected" and paint a clean capsule red.
    total = len(findings)
    if total == 0:
        flags["redaction_coverage"] = 1.0
    else:
        protected = sum(
            1
            for f in findings
            if isinstance(f, dict)
            and (f.get("redaction_strategy") or f.get("action_taken"))
        )
        flags["redaction_coverage"] = protected / total
    return flags


def flags_from_capsule(capsule_dir: Path) -> dict[str, Any]:
    """Collect every trust guarantee a capsule can evidence.

    Keys absent from the result are guarantees the capsule does not speak to;
    the radar renders those as ``n/a``. Never raises: a partial radar is more
    useful than none, and a verification fault must not take the view down.
    """
    flags: dict[str, Any] = {}
    flags.update(_seal_flags(capsule_dir))
    flags.update(_redaction_flags(capsule_dir))
    return flags
