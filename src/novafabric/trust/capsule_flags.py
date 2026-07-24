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

- ``signature_ok`` / ``timestamp_ok`` / ``log_integrity_ok`` — NovaSeal
  verification over the capsule's ``.seal`` directory.
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

log = logging.getLogger(__name__)


def _seal_flags(capsule_dir: Path) -> dict[str, Any]:
    """NovaSeal verification flags, or ``{}`` when the capsule is unsealed."""
    seal_dir = capsule_dir / ".seal"
    if not seal_dir.is_dir():
        return {}  # unsealed → the three seal axes stay n/a, not fail

    capsule_id = capsule_dir.name
    log_file = seal_dir / "log-entry.json"
    if log_file.is_file():
        try:
            entry = json.loads(log_file.read_text())
            capsule_id = entry.get("entry", {}).get("capsule_id", capsule_id)
        except (OSError, ValueError):
            pass  # fall back to the directory name

    # Verification needs the NovaSeal profile (keys, TSA, merkle DB), not just
    # the capsule. Without it we CANNOT verify — which is not the same as
    # verifying and failing, so the axes stay n/a rather than reporting fail.
    try:
        from novafabric.trust.novaseal import KeyConfig, NovaSeal  # noqa: PLC0415
        from novafabric.trust.novaseal.config import (  # noqa: PLC0415
            load_signing_profile,
        )

        profile = load_signing_profile()
        if profile is None:
            return {}
        seal = NovaSeal(
            config=KeyConfig(
                profile=profile.profile,
                key_path=str(profile.key_path),
                cert_path=str(profile.cert_path),
            ),
            tsa_url=profile.tsa_url,
            db_path=str(profile.merkle_db),
        )
        result = seal.verify(capsule_id=capsule_id, seal_dir=str(seal_dir))
    except Exception:  # noqa: BLE001 — a verification fault must not crash the view
        log.warning("seal verification unavailable for %s", capsule_dir, exc_info=True)
        return {}

    return {
        "signature_ok": bool(result.signature_ok),
        "timestamp_ok": bool(result.timestamp_ok),
        "log_integrity_ok": bool(result.log_integrity_ok),
    }


def _redaction_flags(capsule_dir: Path) -> dict[str, Any]:
    """Redaction coverage + secret-scan cleanliness from ``redaction-proof.json``."""
    proof_path = capsule_dir / "redaction-proof.json"
    if not proof_path.is_file():
        return {}  # captured without the masking pipeline → n/a, not fail

    try:
        proof = json.loads(proof_path.read_text())
    except (OSError, ValueError):
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
