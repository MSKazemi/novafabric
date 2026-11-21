"""What a strict mocked replay would do with a capsule, decided when it is written.

Capture and replay were designed separately: a user learned that a capsule cannot
be replayed only by replaying it (``ModelSurfaceNotServable``,
``ToolSurfaceNotServed``, ``CapsuleNotReplayable``, a ``tool_result_not_servable``
divergence). This module classifies every recorded call with the **same
predicates** the replay engine and ``nova replay --dry-run`` use -- it owns no
servability rule of its own:

* model calls -- :func:`novafabric.replay._servability.classify` (ADR-0319);
* tool calls -- :func:`novafabric.replay._contract.tool_surface`,
  :func:`~novafabric.replay._contract.is_not_served_tool_record` and
  :func:`~novafabric.replay._contract.not_servable_reason` (ADR-0306);
* re-execution -- :func:`novafabric.replay._replayability.require_reexecution_plan`
  (ADR-0306 Q7 guards);
* ``replay.yaml`` overrides --
  :meth:`novafabric.replay._policy.PolicyEvaluator.unenforceable_overrides`;
* nested coverage -- :meth:`novafabric.replay._policy.PolicyEvaluator.coverage`
  (ADR-0306 r3c): a call recorded inside a tool call certain to be served is
  never made, so it is counted ``covered`` and refuses nothing.

A change to any of those rules changes the summary with it. Refusals are listed
in the order the engine checks them, so :attr:`ReplayabilitySummary.refusal` is
the error a strict mocked replay of the capsule reports.

The summary is a **prediction by the capturing version**: replay never reads it
and re-classifies the capsule itself, so a newer ``nova replay`` that serves more
surfaces is not bound by it. Pure functions; no import-time IO.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

if TYPE_CHECKING:
    from novafabric.replay._contract import NestedCoverage

Replayable = Literal["full", "partial", "none"]
StrictOutcome = Literal["start", "refuse"]

#: The capsule member the summary is written to.
REPLAYABILITY_REF: Final[str] = "replayability.json"
#: ``schema`` of :data:`REPLAYABILITY_REF`.
REPLAYABILITY_SCHEMA: Final[str] = "novafabric.replayability/1"

#: Reason codes, in the order a strict mocked replay checks them. The first four
#: refuse a strict replay before anything runs; the others let it start.
CAPSULE_NOT_REEXECUTABLE: Final[str] = "capsule_not_reexecutable"
MODEL_SURFACE_NOT_SERVABLE: Final[str] = "model_surface_not_servable"
TOOL_SURFACE_NOT_SERVED: Final[str] = "tool_surface_not_served"
TOOL_OVERRIDE_UNENFORCEABLE: Final[str] = "tool_override_unenforceable"
TOOL_RESULT_NOT_SERVABLE: Final[str] = "tool_result_not_servable"
TOOL_NOT_INTERCEPTED: Final[str] = "tool_not_intercepted"
MODEL_METADATA_CALL_LIVE: Final[str] = "model_metadata_call_live"

#: ``cause`` of a :data:`TOOL_RESULT_NOT_SERVABLE` reason.
CAUSE_PAYLOAD_NOT_RECORDED: Final[str] = "payload_not_recorded"
#: Payloads off, and no recorded model request carries the result (the run's last
#: tool call): the model-request echo cannot recover it (ADR-0306 r3b).
CAUSE_RESULT_NEVER_SENT: Final[str] = "result_never_sent_to_a_model"
CAUSE_OTHER: Final[str] = "other"

#: Most tool names listed per reason.
MAX_LISTED_TOOLS: Final[int] = 10

_REPLAYABLE: frozenset[str] = frozenset({"full", "partial", "none"})
_STRICT: frozenset[str] = frozenset({"start", "refuse"})


class InvalidReplayabilityError(ValueError):
    """A ``replayability.json`` is not well formed."""


@dataclass(frozen=True)
class Reason:
    """One reason a capsule is not fully replayable."""

    code: str
    count: int
    #: A strict mocked replay refuses to start on it.
    refuses_strict: bool
    #: Short phrase for the one-line capture report.
    summary: str
    #: The full explanation (the replay's own wording where it has one).
    detail: str
    extra: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "count": self.count,
            "refuses_strict": self.refuses_strict,
            "summary": self.summary,
            "detail": self.detail,
            **{k: _plain(v) for k, v in self.extra.items()},
        }


@dataclass(frozen=True)
class ReplayabilitySummary:
    """What a strict mocked replay would do with one capsule."""

    replayable: Replayable
    strict_mocked: StrictOutcome
    #: The ``error`` a strict mocked replay records, or ``None`` when it starts.
    refusal: Mapping[str, Any] | None
    reasons: tuple[Reason, ...]
    reexecution: Mapping[str, Any]
    model_calls: Mapping[str, int]
    tool_calls: Mapping[str, int]
    classifier_version: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": REPLAYABILITY_SCHEMA,
            "classifier_version": self.classifier_version,
            "replayable": self.replayable,
            "strict_mocked": self.strict_mocked,
            "refusal": None if self.refusal is None else _plain(self.refusal),
            "reexecution": dict(self.reexecution),
            "model_calls": dict(self.model_calls),
            "tool_calls": dict(self.tool_calls),
            "reasons": [r.as_dict() for r in self.reasons],
        }

    def one_line(self) -> str:
        """The line ``nova capture`` prints."""
        return describe(self.as_dict())


def describe(doc: Mapping[str, Any]) -> str:
    """The one-line report for a summary document (see :meth:`ReplayabilitySummary.as_dict`)."""
    reasons = [r for r in doc.get("reasons") or [] if isinstance(r, Mapping)]
    parts = "; ".join(str(r.get("summary", r.get("code"))) for r in reasons)
    hint = " (see nova replay --dry-run)"
    if doc.get("strict_mocked") == "refuse":
        return f"replay: strict mocked replay would refuse -- {parts}{hint}"
    if reasons:
        return (
            "replay: strict mocked replay would start, but not every recorded call "
            f"is served -- {parts}{hint}"
        )
    models = int((doc.get("model_calls") or {}).get("recorded", 0))
    tools = int((doc.get("tool_calls") or {}).get("recorded", 0))
    return (
        "replay: strict mocked replay would serve every recorded call "
        f"({models} model, {tools} tool)"
    )


def _plain(value: Any) -> Any:
    """JSON-plain copy (tuples and mappings become lists and dicts)."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _tool_names(records: Sequence[Mapping[str, Any]]) -> list[str]:
    return sorted({str(r.get("tool_name")) for r in records})[:MAX_LISTED_TOOLS]


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("novafabric")
    except Exception:  # noqa: BLE001 -- a version label never fails the summary
        return "unknown"


# ── the parts ────────────────────────────────────────────────────────────────


def _reexecution(manifest: Mapping[str, Any]) -> tuple[dict[str, Any], Reason | None, Any]:
    from novafabric.replay._errors import CapsuleNotReplayableError
    from novafabric.replay._replayability import (
        not_reexecutable_reason,
        require_reexecution_plan,
    )

    try:
        plan = require_reexecution_plan(dict(manifest), "mocked")
    except CapsuleNotReplayableError as exc:
        why = not_reexecutable_reason(dict(manifest)) or str(exc)
        summary = (
            f"the capsule cannot be re-run (guard: {exc.guard})" if exc.guard
            else "the capsule records no command to re-run"
        )
        extra = {"guard": exc.guard} if exc.guard else {}
        reason = Reason(CAPSULE_NOT_REEXECUTABLE, 1, True, summary, why, extra)
        state = {"reexecutable": False, "source": None, "guard": exc.guard}
        # The error the real (non-dry) mocked replay records, guard included.
        return state, reason, exc.as_error()
    return {"reexecutable": True, "source": plan.source, "guard": None}, None, None


def _model_part(
    records: Sequence[Any], coverage: NestedCoverage,
) -> tuple[dict[str, int], list[Reason], Any]:
    from novafabric.replay._errors import ModelSurfaceNotServableError
    from novafabric.replay._servability import (
        METADATA,
        NOT_SERVABLE,
        SERVABLE,
        TRANSPORT,
        classify,
        describe_by_system,
        not_servable_by_system,
    )

    # ADR-0306 r3c: a call made inside a tool call that will be served is never
    # made -- counted as covered, in no other class.
    classes = [
        c if c == TRANSPORT or not coverage.is_covered(r) else "covered"
        for r, c in zip(records, classify(records))
    ]
    counts = {
        "recorded": sum(1 for c in classes if c != TRANSPORT),
        "servable": classes.count(SERVABLE),
        "not_servable": classes.count(NOT_SERVABLE),
        "live": classes.count(METADATA),
        "covered": classes.count("covered"),
    }
    reasons: list[Reason] = []
    refusal = None
    by_system = not_servable_by_system(records, coverage)
    if by_system:
        error = ModelSurfaceNotServableError(by_system)
        reasons.append(Reason(
            MODEL_SURFACE_NOT_SERVABLE, error.count, True,
            f"{_plural(error.count, 'model call')} not servable "
            f"({describe_by_system(by_system)})",
            str(error), {"by_system": by_system},
        ))
        refusal = error.as_error()
    if counts["live"]:
        n = counts["live"]
        reasons.append(Reason(
            MODEL_METADATA_CALL_LIVE, n, False,
            f"{_plural(n, 'model-host metadata call')} would run live",
            f"{_plural(n, 'recorded call')} to a model host run no model (an Ollama "
            "/api/tags-style or Bedrock control-plane request): replay does not "
            "serve them, so a replay makes them live (ADR-0319)",
        ))
    return counts, reasons, refusal


def _never_sent(record: Mapping[str, Any]) -> bool:
    """The echo recovery found no recorded model request carrying this result."""
    from novafabric.replay._echo_recovery import ECHO_NEVER_SENT, ECHO_RECOVERY_EXT

    ext = record.get("extensions")
    return isinstance(ext, Mapping) and ext.get(ECHO_RECOVERY_EXT) == ECHO_NEVER_SENT


def _tool_part(
    tool_calls: Sequence[Any], replay_policy: Mapping[str, Any] | None,
    coverage: NestedCoverage,
) -> tuple[dict[str, int], list[Reason], list[tuple[str, Any]]]:
    from novafabric.capture.hooks._python_tool import PAYLOADS_OFF_REASON
    from novafabric.replay._contract import (
        is_not_served_tool_record,
        not_servable_reason,
        not_served_tool_calls,
        tool_surface,
    )
    from novafabric.replay._errors import (
        ReplayOverrideUnenforceableError,
        ToolSurfaceNotServedError,
    )
    from novafabric.replay._flags import ReplayFlags
    from novafabric.replay._policy import PolicyEvaluator

    records = [r for r in tool_calls if isinstance(r, dict)]
    observed: list[dict[str, Any]] = []
    not_intercepted: list[dict[str, Any]] = []
    unservable: dict[str, list[tuple[dict[str, Any], str]]] = {}
    servable = 0
    covered = 0
    for record in records:
        if coverage.is_covered(record):
            # ADR-0306 r3c: never requested; consumed as covered at replay.
            covered += 1
        elif is_not_served_tool_record(record):
            observed.append(record)
        elif tool_surface(record) is None:
            not_intercepted.append(record)
        else:
            why = not_servable_reason(record)
            if why is None:
                servable += 1
            else:
                if _never_sent(record):
                    cause = CAUSE_RESULT_NEVER_SENT
                elif PAYLOADS_OFF_REASON in why:
                    cause = CAUSE_PAYLOAD_NOT_RECORDED
                else:
                    cause = CAUSE_OTHER
                unservable.setdefault(cause, []).append((record, why))
    counts = {
        "recorded": len(records),
        "servable": servable,
        "not_servable": sum(len(v) for v in unservable.values()),
        "observed_only": len(observed),
        "not_intercepted": len(not_intercepted),
        "covered": covered,
    }
    reasons: list[Reason] = []
    refusals: list[tuple[str, Any]] = []

    by_surface = not_served_tool_calls(records, coverage)
    if by_surface:
        error = ToolSurfaceNotServedError(by_surface)
        never_sent = _tool_names([r for r in observed if _never_sent(r)])
        extra: dict[str, Any] = {"tools": _tool_names(observed)}
        tail = ""
        if never_sent:
            extra[CAUSE_RESULT_NEVER_SENT] = never_sent
            tail = f"; result never sent to a model: {', '.join(never_sent)}"
        reasons.append(Reason(
            TOOL_SURFACE_NOT_SERVED, error.count, True,
            f"{_plural(error.count, 'tool call')} observed but not served "
            f"({', '.join(_tool_names(observed))}{tail})",
            str(error), extra,
        ))
        refusals.append((TOOL_SURFACE_NOT_SERVED, error.as_error()))

    evaluator = PolicyEvaluator(dict(replay_policy or {}), ReplayFlags(mode="mocked"))
    unenforceable = evaluator.unenforceable_overrides(records)
    if unenforceable:
        override_error = ReplayOverrideUnenforceableError(unenforceable)
        reasons.append(Reason(
            TOOL_OVERRIDE_UNENFORCEABLE, len(unenforceable), True,
            f"{_plural(len(unenforceable), 'replay.yaml override')} cannot be enforced",
            str(override_error),
            {"tools": sorted({str(u.get("tool_name")) for u in unenforceable})},
        ))
        refusals.append((TOOL_OVERRIDE_UNENFORCEABLE, override_error.as_error()))

    for cause in (CAUSE_RESULT_NEVER_SENT, CAUSE_PAYLOAD_NOT_RECORDED, CAUSE_OTHER):
        entries = unservable.get(cause)
        if not entries:
            continue
        names = _tool_names([r for r, _ in entries])
        n = len(entries)
        whys = sorted({why for _, why in entries})
        what = {
            CAUSE_RESULT_NEVER_SENT: (
                f"{_plural(n, 'tool call')} whose result was never sent to a model "
                "and not recorded"
            ),
            CAUSE_PAYLOAD_NOT_RECORDED: f"{_plural(n, 'tool call')} whose payload was not recorded",
        }.get(cause, f"{_plural(n, 'tool call')} whose result is not servable")
        reasons.append(Reason(
            TOOL_RESULT_NOT_SERVABLE, n, False, f"{what} ({', '.join(names)})",
            "a strict replay starts and refuses each call when it is made "
            f"(tool_result_not_servable): {'; '.join(whys)}",
            {"cause": cause, "tools": names},
        ))

    if not_intercepted:
        by_transport: dict[str, int] = {}
        for record in not_intercepted:
            transport = str(record.get("transport") or "unknown")
            by_transport[transport] = by_transport.get(transport, 0) + 1
        n = len(not_intercepted)
        reasons.append(Reason(
            TOOL_NOT_INTERCEPTED, n, False,
            f"{_plural(n, 'tool call')} on a transport replay does not intercept "
            f"({', '.join(f'{k}: {v}' for k, v in sorted(by_transport.items()))})",
            "replay neither serves nor stops these calls: replayed, they run live "
            "(only MCP tools/call and record.tool-style surfaces are intercepted; "
            "ADR-0306)",
            {"by_transport": dict(sorted(by_transport.items())),
             "tools": _tool_names(not_intercepted)},
        ))
    return counts, reasons, refusals


# ── the whole capsule ────────────────────────────────────────────────────────

#: Order a strict mocked replay checks its refusals in (``_engine._mocked``).
_REFUSAL_ORDER: tuple[str, ...] = (
    CAPSULE_NOT_REEXECUTABLE, MODEL_SURFACE_NOT_SERVABLE, TOOL_SURFACE_NOT_SERVED,
    TOOL_OVERRIDE_UNENFORCEABLE,
)
_REASON_ORDER: tuple[str, ...] = (
    *_REFUSAL_ORDER, TOOL_RESULT_NOT_SERVABLE, TOOL_NOT_INTERCEPTED, MODEL_METADATA_CALL_LIVE,
)


def assess(
    manifest: Mapping[str, Any],
    model_records: Sequence[Any],
    tool_calls: Sequence[Any],
    *,
    replay_policy: Mapping[str, Any] | None = None,
) -> ReplayabilitySummary:
    """Classify a capsule's recorded calls as a strict mocked replay would.

    *model_records* is the whole ``model-calls.jsonl`` (transport records
    included: ADR-0305 roles depend on the records around them).
    """
    from novafabric.replay._echo_recovery import recover_tool_results_from_echo
    from novafabric.replay._flags import ReplayFlags
    from novafabric.replay._policy import PolicyEvaluator

    reexecution, reexec_reason, reexec_refusal = _reexecution(manifest)
    # Same step the engine runs before classifying (ADR-0306 r3): a payloads-off
    # tool result the next model request echoes under its digest is servable.
    tool_calls = recover_tool_results_from_echo(
        [r for r in tool_calls if isinstance(r, dict)],
        [r for r in model_records if isinstance(r, dict)],
    )
    # The coverage a strict mocked replay computes (ADR-0306 r3c), from the same
    # evaluator the engine and --dry-run use.
    coverage = PolicyEvaluator(
        dict(replay_policy or {}), ReplayFlags(mode="mocked")
    ).coverage(tool_calls)
    model_counts, model_reasons, model_refusal = _model_part(model_records, coverage)
    tool_counts, tool_reasons, tool_refusals = _tool_part(tool_calls, replay_policy, coverage)

    reasons = [*([reexec_reason] if reexec_reason else []), *model_reasons, *tool_reasons]
    reasons.sort(key=lambda r: _REASON_ORDER.index(r.code))
    refusals: dict[str, Any] = dict(tool_refusals)
    if reexec_refusal is not None:
        refusals[CAPSULE_NOT_REEXECUTABLE] = reexec_refusal
    if model_refusal is not None:
        refusals[MODEL_SURFACE_NOT_SERVABLE] = model_refusal
    refusal = next((refusals[c] for c in _REFUSAL_ORDER if c in refusals), None)

    replayable: Replayable
    if reexec_reason is not None:
        replayable = "none"
    elif reasons:
        replayable = "partial"
    else:
        replayable = "full"
    return ReplayabilitySummary(
        replayable=replayable,
        strict_mocked="refuse" if refusal is not None else "start",
        refusal=refusal,
        reasons=tuple(reasons),
        reexecution=reexecution,
        model_calls=model_counts,
        tool_calls=tool_counts,
        classifier_version=_version(),
    )


def render(summary: ReplayabilitySummary) -> str:
    """The bytes of :data:`REPLAYABILITY_REF` (stable key order)."""
    return json.dumps(summary.as_dict(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def parse_replayability(data: bytes) -> dict[str, Any]:
    """A :data:`REPLAYABILITY_REF` as a dict; raises :class:`InvalidReplayabilityError`."""
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidReplayabilityError(f"{REPLAYABILITY_REF} is not UTF-8 JSON") from exc
    if not isinstance(doc, dict) or doc.get("schema") != REPLAYABILITY_SCHEMA:
        raise InvalidReplayabilityError(f"{REPLAYABILITY_REF} has an unknown schema")
    if doc.get("replayable") not in _REPLAYABLE or doc.get("strict_mocked") not in _STRICT:
        raise InvalidReplayabilityError(
            f"{REPLAYABILITY_REF} lacks a valid replayable/strict_mocked verdict"
        )
    if not isinstance(doc.get("reasons", []), list):
        raise InvalidReplayabilityError(f"{REPLAYABILITY_REF} reasons is not a list")
    return doc


def _read_jsonl(capsule_dir: Path, name: str, operation: str) -> list[Any]:
    """Records of a capsule JSONL stream, as the replay engine reads them: absent =
    none; an unusable member (a link, FIFO) raises; an undecodable line is skipped."""
    from novafabric._capsule_fs import read_required_capsule_member

    text = read_required_capsule_member(capsule_dir / name, operation=operation)
    records: list[Any] = []
    for line in (text or "").splitlines():
        if line.strip():
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def assess_capsule(
    capsule_dir: Path, manifest: Mapping[str, Any], *, operation: str = "replayability"
) -> ReplayabilitySummary:
    """:func:`assess` over the capsule's files (and its in-memory *manifest*)."""
    import yaml

    from novafabric._capsule_fs import read_required_capsule_member

    policy_text = read_required_capsule_member(capsule_dir / "replay.yaml", operation=operation)
    policy = yaml.safe_load(policy_text) if policy_text else None
    return assess(
        manifest,
        _read_jsonl(capsule_dir, "model-calls.jsonl", operation),
        _read_jsonl(capsule_dir, "tool-calls.jsonl", operation),
        replay_policy=policy if isinstance(policy, dict) else None,
    )


def read_replayability(capsule_dir: Path) -> dict[str, Any] | None:
    """The capsule's summary; ``None`` for a capsule written without one (unknown)."""
    from novafabric._capsule_fs import read_required_capsule_member

    text = read_required_capsule_member(
        capsule_dir / REPLAYABILITY_REF, operation="read replayability"
    )
    if text is None:
        return None
    return parse_replayability(text.encode("utf-8"))
