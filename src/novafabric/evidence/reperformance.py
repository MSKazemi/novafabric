"""Re-performance attestation (ADR-0087(c), gap-008).

A DSSE-signed statement that a replay of run X at time T reproduced outcome
digest D under replay mode M with verdict V — the FAccT "re-performance of a
computation" evidence, and the substrate for the gap-010 incident track.

The mode and verdict are recorded separately so verifiers can weight
``exact`` and ``semantic-match`` differently (a ``semantic`` replay embeds a
judgment call; the attestation never hides that).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from novafabric.evidence.intoto import dsse_sign, make_intoto_statement
from novafabric.replay._result import ReplayResult

REPERFORMANCE_PREDICATE_TYPE = "https://novafabric.io/reperformance/v0"

MatchVerdict = Literal["exact", "semantic-match", "mismatch"]


class ReperformanceAttestation(BaseModel):
    """Signed claim of a completed replay and its outcome (ADR-0087(c))."""

    schema_version: str = "0.1.0"
    run_id: str
    capsule_hash: str
    replayed_at: str
    replay_mode: str
    outcome_digest: str
    match: MatchVerdict
    replayer: dict[str, str] = Field(
        default_factory=lambda: {"name": "novafabric", "version": "unknown"}
    )
    notes: str | None = None


def outcome_digest_of(result: ReplayResult) -> str:
    """Stable digest over the replay's outcome facets (slice-1 definition).

    Deliberately excludes wall-clock fields (start/end/duration) so the same
    outcome always digests identically.
    """
    facets = {
        "replay_of_run_id": result.replay_of_run_id,
        "mode": result.mode,
        "status": result.status,
        "exit_code": result.exit_code,
        "model_calls_mocked": result.model_calls_mocked,
        "tool_calls_mocked": result.tool_calls_mocked,
    }
    canonical = json.dumps(facets, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def verdict_for(result: ReplayResult) -> MatchVerdict:
    """Slice-1 verdict derivation, recorded alongside the mode.

    ``exact``-mode success is an exact match; success in any other mode is a
    semantic match (the mode field lets verifiers weight it); anything else
    is a mismatch.
    """
    if result.status != "success":
        return "mismatch"
    return "exact" if result.mode == "exact" else "semantic-match"


def build_reperformance_attestation(
    capsule_dir: Path,
    result: ReplayResult,
    replayer_version: str = "unknown",
    notes: str | None = None,
) -> ReperformanceAttestation:
    """Build the attestation from a completed :class:`ReplayResult`."""
    manifest_path = capsule_dir / "capsule.yaml"
    capsule_hash = hashlib.sha256(
        manifest_path.read_bytes() if manifest_path.exists() else b""
    ).hexdigest()
    return ReperformanceAttestation(
        run_id=result.replay_of_run_id,
        capsule_hash=capsule_hash,
        replayed_at=datetime.now(timezone.utc).isoformat(),
        replay_mode=result.mode,
        outcome_digest=outcome_digest_of(result),
        match=verdict_for(result),
        replayer={"name": "novafabric", "version": replayer_version},
        notes=notes,
    )


def sign_reperformance(
    attestation: ReperformanceAttestation, signer: Any
) -> dict[str, Any]:
    """Wrap the attestation in an in-toto statement and DSSE-sign it."""
    statement = make_intoto_statement(
        predicate_type=REPERFORMANCE_PREDICATE_TYPE,
        subject_name=f"run-capsule/{attestation.run_id}",
        subject_sha256=attestation.capsule_hash,
        predicate=attestation.model_dump(),
    )
    envelope: dict[str, Any] = dsse_sign(statement, signer)
    return envelope
