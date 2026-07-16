"""Session replay orchestrator (ADR-0123 P1 + divergence policy, experimental).

Replays every member capsule of an ADR-0122 session in ascending ``sequence``
order by invoking the **existing** per-capsule replay engine
(``novafabric.replay``) once per turn — no new replay mode, no change to any
per-capsule contract, no bypass of the inherited safety defaults. The output
is one additive ``SessionReplayResult`` record (schema
``schemas/session-replay-result.schema.json``, v0.1.0).

Honesty rules (ADR-0123 D5):

- a member that is ``missing`` or ``tampered`` (per the ADR-0122 view
  resolution) is a **hard refusal** — recorded, never silently skipped;
- a hard refusal halts the session unless ``continue_past_refusal`` is set
  (which is itself logged into the result);
- a soft divergence (the re-executed turn exited non-zero) halts under the
  default ``on_divergence="stop"`` and may be continued past with
  ``on_divergence="continue"``;
- turns after a halt are **absent** from ``turns`` (not ``skipped``).

Implemented here: the ordered driver, the four per-turn modes, and the
divergence policy (ADR-0123 P1/P3-subset). Still future design: the
content-addressed state-seam verification between turns (P2 — the
``state_*`` fields are emitted ``null``), the composed session attestation
(P4), sub-range replay and ``--dry-run`` (P5), and the session-wide cost
ceiling.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from novafabric.replay._engine import ReplayEngine
from novafabric.replay._flags import ReplayFlags
from novafabric.session.manifest import (
    SessionError,
    SessionIntegrityError,
    load_session,
    session_manifest_path,
)
from novafabric.session.view import ResolvedMember, resolve_members

SESSION_REPLAY_SCHEMA_VERSION = "0.1.0"
SESSION_REPLAY_RESULT_FILENAME = "session_replay_result.json"

SessionReplayMode = Literal["forensic", "mocked", "semantic", "exact"]
DivergencePolicy = Literal["stop", "continue"]
TurnStatus = Literal["reproduced", "diverged", "refused", "skipped"]
SessionVerdict = Literal["reproduced", "diverged", "refused", "partial"]


class SessionReplayError(SessionError):
    """The session cannot be replayed at all (e.g. it has no members)."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class TurnReplayResult(BaseModel):
    """One per-turn verdict — one member capsule through the replay engine."""

    sequence: int
    source_capsule_id: str
    effective_mode: SessionReplayMode
    status: TurnStatus
    replay_capsule_id: str | None = None
    #: State-seam fields are future design (ADR-0123 P2): always ``None`` today.
    state_in_hash: str | None = None
    state_out_hash: str | None = None
    state_seam_match: bool | None = None
    divergence: dict[str, str] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        # The graduated schema requires every nullable key to be present.
        return {
            "sequence": self.sequence,
            "source_capsule_id": self.source_capsule_id,
            "effective_mode": self.effective_mode,
            "status": self.status,
            "replay_capsule_id": self.replay_capsule_id,
            "state_in_hash": self.state_in_hash,
            "state_out_hash": self.state_out_hash,
            "state_seam_match": self.state_seam_match,
            "divergence": dict(self.divergence) if self.divergence else None,
        }


class SessionReplayResult(BaseModel):
    """One record per session replay — ordered turn verdicts + one aggregate."""

    schema_version: str = SESSION_REPLAY_SCHEMA_VERSION
    session_id: str
    session_manifest_hash: str
    mode: SessionReplayMode
    on_divergence: DivergencePolicy
    whole_session_verdict: SessionVerdict
    turns: list[TurnReplayResult]
    started_at: str
    finished_at: str
    continue_past_refusal: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "session_manifest_hash": self.session_manifest_hash,
            "mode": self.mode,
            "on_divergence": self.on_divergence,
            "whole_session_verdict": self.whole_session_verdict,
            "turns": [t.to_json_dict() for t in self.turns],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
        if self.continue_past_refusal:
            # Optional field, logged only when the override was actually used.
            data["continue_past_refusal"] = True
        return data


def _refused_turn(
    resolved: ResolvedMember, mode: SessionReplayMode, detail: str
) -> TurnReplayResult:
    return TurnReplayResult(
        sequence=resolved.member.sequence,
        source_capsule_id=resolved.member.run_id,
        effective_mode=mode,
        status="refused",
        replay_capsule_id=None,
        divergence={"kind": "precondition_refusal", "detail": detail},
    )


def _replay_turn(
    resolved: ResolvedMember, mode: SessionReplayMode, base_dir: Path
) -> TurnReplayResult:
    """Replay one resolved member through the existing per-capsule engine."""
    member = resolved.member
    if resolved.status == "missing":
        return _refused_turn(
            resolved,
            mode,
            f"member capsule could not be located (capsule_ref "
            f"{member.capsule_ref}) — a session with a missing member "
            "cannot honestly claim reproduction",
        )
    if resolved.status == "tampered":
        return _refused_turn(
            resolved,
            mode,
            f"member capsule at {resolved.capsule_dir} no longer matches its "
            f"recorded content digest (capsule_ref {member.capsule_ref}) — "
            "refusing to replay tampered evidence",
        )

    assert resolved.capsule_dir is not None  # status == "ok" implies located
    engine = ReplayEngine(
        capsule_dir=Path(resolved.capsule_dir),
        flags=ReplayFlags(mode=mode),
        base_dir=base_dir,
    )
    result = engine.run()

    if mode == "exact" and result.exact_eligible is False:
        reasons = "; ".join(result.exact_reasons or []) or "exact preconditions not met"
        return _refused_turn(resolved, mode, f"exact-mode refusal: {reasons}")
    if result.status == "aborted":
        message = (result.error or {}).get("message", "replay aborted")
        return _refused_turn(resolved, mode, str(message))
    if result.status == "success":
        return TurnReplayResult(
            sequence=member.sequence,
            source_capsule_id=member.run_id,
            effective_mode=mode,
            status="reproduced",
            replay_capsule_id=result.replay_id,
        )
    # Non-zero exit (or any other engine failure): the turn re-executed but
    # did not reproduce — a soft divergence, localized to this turn.
    message = (result.error or {}).get("message", f"replay status {result.status}")
    return TurnReplayResult(
        sequence=member.sequence,
        source_capsule_id=member.run_id,
        effective_mode=mode,
        status="diverged",
        replay_capsule_id=result.replay_id,
        divergence={"kind": "replay_failed", "detail": str(message)},
    )


def _whole_session_verdict(turns: list[TurnReplayResult]) -> SessionVerdict:
    if any(t.status == "refused" for t in turns):
        return "refused"
    if any(t.status != "reproduced" for t in turns):
        return "diverged"
    return "reproduced"


def replay_session(
    session_id: str,
    mode: SessionReplayMode = "mocked",
    on_divergence: DivergencePolicy = "stop",
    continue_past_refusal: bool = False,
    root: Path | None = None,
    capsule_base: Path | None = None,
    base_dir: Path | None = None,
) -> SessionReplayResult:
    """Replay every member of *session_id* in ``sequence`` order.

    Each turn goes through the existing per-capsule replay engine in *mode*
    (default ``mocked`` — safe, offline, deterministic by construction). The
    per-turn replay capsules land under *base_dir* exactly as single-capsule
    ``nova replay`` output does.

    Raises:
        SessionNotFoundError: No manifest exists for *session_id*.
        SessionIntegrityError: The manifest is malformed, breaks ordering, or
            has gaps in its ``sequence`` values (replay refuses; the manifest
            is never repaired).
        SessionReplayError: The session has no members — nothing to replay.
    """
    manifest = load_session(session_id, root=root)
    if not manifest.member_runs:
        raise SessionReplayError(
            f"session {session_id} has no member runs — nothing to replay"
        )
    sequences = sorted(m.sequence for m in manifest.member_runs)
    if any(b != a + 1 for a, b in zip(sequences, sequences[1:])):
        raise SessionIntegrityError(
            f"session {session_id}: member sequences {sequences} have gaps — "
            "an incomplete session cannot be replayed as a unit"
        )

    manifest_bytes = session_manifest_path(session_id, root).read_bytes()
    manifest_hash = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    resolved = resolve_members(manifest, root=root, capsule_base=capsule_base)
    replays_dir = base_dir or (Path.cwd() / ".novafabric" / "replays")

    started_at = _now()
    turns: list[TurnReplayResult] = []
    for member in resolved:
        turn = _replay_turn(member, mode, replays_dir)
        turns.append(turn)
        if turn.status == "refused" and not continue_past_refusal:
            break  # hard refusal: never silently proceed (ADR-0123 D5)
        if turn.status == "diverged" and on_divergence == "stop":
            break  # soft divergence under the default stop policy

    return SessionReplayResult(
        session_id=session_id,
        session_manifest_hash=manifest_hash,
        mode=mode,
        on_divergence=on_divergence,
        whole_session_verdict=_whole_session_verdict(turns),
        turns=turns,
        started_at=started_at,
        finished_at=_now(),
        continue_past_refusal=continue_past_refusal,
    )


def write_session_replay_result(
    result: SessionReplayResult, output_dir: Path
) -> Path:
    """Persist one ``SessionReplayResult`` as JSON; returns the file path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / SESSION_REPLAY_RESULT_FILENAME
    path.write_text(
        json.dumps(result.to_json_dict(), indent=2) + "\n", encoding="utf-8"
    )
    return path
