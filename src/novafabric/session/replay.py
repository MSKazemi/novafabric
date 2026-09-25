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

Sub-range + per-turn policy + dry-run (ADR-0123 P5, experimental):

- ``from_seq``/``to_seq`` replay one contiguous, inclusive slice of the
  session; the slice is recorded as the optional ``range`` field so a
  consumer knows the verdict covers a slice, not the whole session;
- ``turn_modes`` pins a mode per turn (D6); every other turn uses the
  session-level mode, each turn's ``effective_mode`` records what ran, and
  the pins themselves are logged as the optional ``turn_mode_policy`` field;
- :func:`plan_session_replay` computes the same selection **without
  executing anything** — member order, per-turn effective mode, integrity
  pre-check, and mutating-tool exposure under the inherited per-capsule
  safety policy (ADR-0012).

Implemented here: the ordered driver, the four per-turn modes, the
divergence policy (ADR-0123 P1/P3-subset) and P5. Still future design: the
content-addressed state-seam verification between turns (P2 — the
``state_*`` fields are emitted ``null``, so a sub-range's boundary turn is
replayed from its captured inputs exactly like every other turn), the
composed session attestation (P4), and the session-wide cost ceiling.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from novafabric.replay._engine import ReplayEngine, _load_replay_policy, _read_jsonl
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._policy import PolicyEvaluator
from novafabric.session.manifest import (
    SessionError,
    SessionIntegrityError,
    SessionManifest,
    load_session,
    session_manifest_path,
)
from novafabric.session.view import MemberStatus, ResolvedMember, resolve_members

SESSION_REPLAY_SCHEMA_VERSION = "0.1.0"
SESSION_REPLAY_RESULT_FILENAME = "session_replay_result.json"

SessionReplayMode = Literal["forensic", "mocked", "semantic", "exact"]
DivergencePolicy = Literal["stop", "continue"]
TurnStatus = Literal["reproduced", "diverged", "refused", "skipped"]
SessionVerdict = Literal["reproduced", "diverged", "refused", "partial"]


SESSION_REPLAY_PLAN_VERSION = "0.1.0"
#: Mutation classes that are *not* mutating (ADR-0012 safety ladder).
_NON_MUTATING_CLASSES = frozenset({"none", "read-only"})


class SessionReplayError(SessionError):
    """The session cannot be replayed at all (e.g. it has no members)."""


class SessionReplayRangeError(SessionReplayError):
    """``--from``/``--to`` or a per-turn mode pin names turns the session lacks."""


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
    #: Inclusive ``(from, to)`` sequence slice when a sub-range was replayed.
    range: tuple[int, int] | None = None
    #: Per-turn mode pins (D6) that were in force, keyed by sequence.
    turn_mode_policy: dict[int, SessionReplayMode] | None = None

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
        if self.range is not None:
            data["range"] = {"from": self.range[0], "to": self.range[1]}
        if self.turn_mode_policy:
            data["turn_mode_policy"] = {
                str(seq): mode for seq, mode in sorted(self.turn_mode_policy.items())
            }
        return data


class ToolExposure(BaseModel):
    """Recorded tool calls of one turn, classified by the inherited replay policy."""

    tool_calls: int = 0
    #: Calls whose ``mutation_class`` is anything but ``none``/``read-only``.
    mutating: int = 0
    #: Per-capsule policy decision counts (``mock``/``allow``/``deny``).
    decisions: dict[str, int] = Field(default_factory=dict)
    #: Count per recorded ``mutation_class``.
    mutation_classes: dict[str, int] = Field(default_factory=dict)


class TurnReplayPlan(BaseModel):
    """What one turn *would* do — computed without executing it."""

    sequence: int
    source_capsule_id: str
    effective_mode: SessionReplayMode
    #: Whether the mode came from a per-turn pin rather than the session mode.
    mode_pinned: bool
    integrity: MemberStatus
    #: True when the pre-check already guarantees a hard refusal
    #: (``missing``/``tampered``). Engine-time refusals (e.g. ``exact``
    #: preconditions) are only known on execution and are not predicted.
    would_refuse: bool
    tool_exposure: ToolExposure | None = None


class SessionReplayPlan(BaseModel):
    """``nova session replay --dry-run`` output: the plan, nothing executed."""

    plan_version: str = SESSION_REPLAY_PLAN_VERSION
    session_id: str
    session_manifest_hash: str
    mode: SessionReplayMode
    on_divergence: DivergencePolicy
    continue_past_refusal: bool
    range: tuple[int, int] | None = None
    turn_mode_policy: dict[int, SessionReplayMode] | None = None
    total_turns: int
    turns: list[TurnReplayPlan]

    def to_json_dict(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["range"] = {"from": self.range[0], "to": self.range[1]} if self.range else None
        data["turn_mode_policy"] = (
            {str(k): v for k, v in sorted(self.turn_mode_policy.items())}
            if self.turn_mode_policy
            else None
        )
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


def _load_replayable(session_id: str, root: Path | None) -> tuple[SessionManifest, str]:
    """Load the manifest, refuse empty/gapped sessions, and pin its hash."""
    manifest = load_session(session_id, root=root)
    if not manifest.member_runs:
        raise SessionReplayError(f"session {session_id} has no member runs — nothing to replay")
    sequences = sorted(m.sequence for m in manifest.member_runs)
    if any(b != a + 1 for a, b in zip(sequences, sequences[1:])):
        raise SessionIntegrityError(
            f"session {session_id}: member sequences {sequences} have gaps — "
            "an incomplete session cannot be replayed as a unit"
        )
    manifest_bytes = session_manifest_path(session_id, root).read_bytes()
    return manifest, "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()


def select_range(
    manifest: SessionManifest,
    from_seq: int | None,
    to_seq: int | None,
    turn_modes: Mapping[int, SessionReplayMode] | None,
) -> tuple[int, int] | None:
    """Validate a sub-range and per-turn pins; return the inclusive slice.

    Returns ``None`` when neither bound is given (the whole session). A single
    bound extends to the session's first/last turn.

    Raises:
        SessionReplayRangeError: A bound is outside the session, ``from`` is
            after ``to``, or a pin names a turn that is absent from the
            session or outside the selected slice (a pin that would silently
            never apply is refused, not ignored).
    """
    sequences = [m.sequence for m in manifest.member_runs]
    first, last = min(sequences), max(sequences)
    selected: tuple[int, int] | None = None
    if from_seq is not None or to_seq is not None:
        lo = first if from_seq is None else from_seq
        hi = last if to_seq is None else to_seq
        for label, value in (("--from", lo), ("--to", hi)):
            if value < first or value > last:
                raise SessionReplayRangeError(
                    f"{label} {value} is outside session {manifest.session_id} "
                    f"(turns {first}..{last})"
                )
        if lo > hi:
            raise SessionReplayRangeError(f"--from {lo} is after --to {hi}")
        selected = (lo, hi)
    lo, hi = selected or (first, last)
    for seq in sorted(turn_modes or {}):
        if seq not in sequences:
            raise SessionReplayRangeError(
                f"turn-mode pin for turn {seq}: session {manifest.session_id} "
                f"has no such turn (turns {first}..{last})"
            )
        if not lo <= seq <= hi:
            raise SessionReplayRangeError(
                f"turn-mode pin for turn {seq} is outside the selected range "
                f"{lo}..{hi} and would never apply"
            )
    return selected


def _in_range(member: ResolvedMember, selected: tuple[int, int] | None) -> bool:
    return selected is None or selected[0] <= member.member.sequence <= selected[1]


def replay_session(
    session_id: str,
    mode: SessionReplayMode = "mocked",
    on_divergence: DivergencePolicy = "stop",
    continue_past_refusal: bool = False,
    root: Path | None = None,
    capsule_base: Path | None = None,
    base_dir: Path | None = None,
    from_seq: int | None = None,
    to_seq: int | None = None,
    turn_modes: Mapping[int, SessionReplayMode] | None = None,
) -> SessionReplayResult:
    """Replay the members of *session_id* in ``sequence`` order.

    Each turn goes through the existing per-capsule replay engine in *mode*
    (default ``mocked`` — safe, offline, deterministic by construction), or
    in the mode pinned for it in *turn_modes*. The per-turn replay capsules
    land under *base_dir* exactly as single-capsule ``nova replay`` output
    does. *from_seq*/*to_seq* (inclusive) restrict the run to one contiguous
    slice; the slice is recorded in the result's ``range``.

    Raises:
        SessionNotFoundError: No manifest exists for *session_id*.
        SessionIntegrityError: The manifest is malformed, breaks ordering, or
            has gaps in its ``sequence`` values (replay refuses; the manifest
            is never repaired).
        SessionReplayError: The session has no members — nothing to replay.
        SessionReplayRangeError: The sub-range or a per-turn pin is invalid.
    """
    manifest, manifest_hash = _load_replayable(session_id, root)
    selected = select_range(manifest, from_seq, to_seq, turn_modes)
    pins = dict(turn_modes or {})
    resolved = resolve_members(manifest, root=root, capsule_base=capsule_base)
    replays_dir = base_dir or (Path.cwd() / ".novafabric" / "replays")

    started_at = _now()
    turns: list[TurnReplayResult] = []
    for member in resolved:
        if not _in_range(member, selected):
            continue
        effective = pins.get(member.member.sequence, mode)
        turn = _replay_turn(member, effective, replays_dir)
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
        range=selected,
        turn_mode_policy=pins or None,
    )


def _tool_exposure(capsule_dir: Path, mode: SessionReplayMode) -> ToolExposure:
    """Classify recorded tool calls under the per-capsule policy (read-only)."""
    tool_calls = _read_jsonl(capsule_dir / "tool-calls.jsonl")
    evaluator = PolicyEvaluator(_load_replay_policy(capsule_dir), ReplayFlags(mode=mode))
    exposure = ToolExposure(tool_calls=len(tool_calls))
    for decision in evaluator.check_all(tool_calls):
        exposure.decisions[decision.decision] = exposure.decisions.get(decision.decision, 0) + 1
        klass = str(decision.mutation_class)
        exposure.mutation_classes[klass] = exposure.mutation_classes.get(klass, 0) + 1
        if klass not in _NON_MUTATING_CLASSES:
            exposure.mutating += 1
    return exposure


def plan_session_replay(
    session_id: str,
    mode: SessionReplayMode = "mocked",
    on_divergence: DivergencePolicy = "stop",
    continue_past_refusal: bool = False,
    root: Path | None = None,
    capsule_base: Path | None = None,
    from_seq: int | None = None,
    to_seq: int | None = None,
    turn_modes: Mapping[int, SessionReplayMode] | None = None,
) -> SessionReplayPlan:
    """The replay plan for *session_id* — nothing is executed or written.

    Applies exactly the validation and selection :func:`replay_session` would
    (same errors), then reports each selected turn's effective mode, member
    integrity, and tool exposure. A malformed ``replay.yaml`` or unreadable
    tool log never fails the plan; that turn's exposure is left ``None``.
    """
    manifest, manifest_hash = _load_replayable(session_id, root)
    selected = select_range(manifest, from_seq, to_seq, turn_modes)
    pins = dict(turn_modes or {})
    turns: list[TurnReplayPlan] = []
    for member in resolve_members(manifest, root=root, capsule_base=capsule_base):
        if not _in_range(member, selected):
            continue
        seq = member.member.sequence
        effective = pins.get(seq, mode)
        exposure: ToolExposure | None = None
        if member.status == "ok" and member.capsule_dir is not None:
            try:
                exposure = _tool_exposure(Path(member.capsule_dir), effective)
            except (OSError, ValueError, AttributeError, TypeError, yaml.YAMLError):
                exposure = None  # plan output must never fail on one bad log
        turns.append(
            TurnReplayPlan(
                sequence=seq,
                source_capsule_id=member.member.run_id,
                effective_mode=effective,
                mode_pinned=seq in pins,
                integrity=member.status,
                would_refuse=member.status != "ok",
                tool_exposure=exposure,
            )
        )
    return SessionReplayPlan(
        session_id=session_id,
        session_manifest_hash=manifest_hash,
        mode=mode,
        on_divergence=on_divergence,
        continue_past_refusal=continue_past_refusal,
        range=selected,
        turn_mode_policy=pins or None,
        total_turns=len(manifest.member_runs),
        turns=turns,
    )


def write_session_replay_result(result: SessionReplayResult, output_dir: Path) -> Path:
    """Persist one ``SessionReplayResult`` as JSON; returns the file path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / SESSION_REPLAY_RESULT_FILENAME
    path.write_text(json.dumps(result.to_json_dict(), indent=2) + "\n", encoding="utf-8")
    return path
