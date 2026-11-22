"""Outcome equivalence of a mocked replay with the run it replays (ADR-0321).

A replay's ``status`` says how the replayed *process* ended. It does not say
whether the replay reproduced the recorded run: a replay that repeated a side
effect or called a live service can exit 0, and a faithful replay of a run that
failed exits non-zero. The ``fidelity`` block answers the second question by
comparing the replay with what the capsule recorded:

* ``exit_code`` -- the replayed exit code against ``capsule.yaml: exit_code``;
* ``stdout_normalized_sha256`` -- the replayed stdout against
  ``outputs/stdout.txt`` after the declared volatile-token rule set
  (``replay/_normalize.py``, ``volatile-ids/1``: RFC 4122 UUIDs). Required;
* ``stdout_sha256`` / ``stderr_sha256`` -- the replayed output streams against
  ``outputs/stdout.txt`` / ``outputs/stderr.txt``, byte for byte. Both are
  **advisory**: compared and reported (``advisory: true``) but they never decide
  the verdict -- stdout because a workload prints ids it generates (the
  normalised check decides), stderr because workloads write timestamped logs
  there by convention (owner decision 2026-11-19, ADR-0321);
* ``file_effects`` -- every file the recorded workload wrote
  (``file-effects.json``, ``capture/file_effects.py``) that the replay wrote
  again must hold, after the replay, the content it held after the recording: a
  replay that appends to the file again or writes something else diverges.
  Required; a capsule with no record lists it in ``unchecked``;
* ``file_writes_not_recorded`` -- files only the replay wrote, listed by path;
  advisory (such names are often volatile). A recorded write the replay did not
  repeat is not judged: it cannot be told from a served tool's effect;
* ``model_call_sequence`` / ``tool_call_sequence`` -- the calls the replay
  served, in the order it served them, against the recorded order. Model calls
  are compared in strict order; tool calls in strict order *across* model turns
  but as a multiset *within* one (the calls between two consecutive model calls,
  which answer one model response and may run in parallel); recorded turns come
  from the records' ``started_at`` (``comparison: per-model-turn-multiset``), and
  without usable timestamps the order is strict (``comparison: strict-order``);
* ``divergence`` -- the replay contract's divergence state (ADR-0300);
* ``live_calls`` -- calls the replay reported going live or could not intercept;
* ``tool_result_echo`` -- the report-only echo check (ADR-0306 D10).

Only ``stdout_normalized_sha256`` is normalised, and only by the versioned rule
set the result names in ``normalization``; a timestamp the workload prints still
makes its replay ``diverged``. A check that cannot be computed -- the capsule
never recorded the data -- is ``equal: null``, and a verdict is never
``faithful`` by default.

Pure functions over data the engine already holds, plus capsule reads that never
follow a link (``novafabric._capsule_fs``); no import-time IO.
"""

from __future__ import annotations

import hashlib
import json
import os
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from novafabric._capsule_fs import (
    CapsuleRequiredFileError,
    read_required_capsule_file,
)
from novafabric.capture.file_effects import (
    FILE_EFFECTS_REF,
    UNCOMPARABLE_STATES,
    FileState,
    InvalidFileEffectsError,
    display_path,
    file_state,
    parse_record,
    replay_written_paths,
    resolve_recorded_path,
)
from novafabric.replay._contract import (
    MODEL_SURFACES,
    TOOL_SURFACE_MCP,
    ReplayContractReport,
    interceptable_tool_calls,
    is_interceptable_tool_call,
    model_queue_key,
    model_queues,
    served_model_records,
    tool_surface,
)
from novafabric.replay._normalize import NORMALIZER_ID, normalize_volatile
from novafabric.replay._tool_call_outcomes import collapse_tool_divergences

VERDICT_FAITHFUL = "faithful"
VERDICT_DIVERGED = "diverged"
VERDICT_FAILED = "failed"
VERDICT_NOT_COMPARED = "not_compared"
VERDICTS: tuple[str, ...] = (
    VERDICT_FAITHFUL, VERDICT_DIVERGED, VERDICT_FAILED, VERDICT_NOT_COMPARED,
)

#: ``normalization`` of a result in which no check normalised anything (a replay
#: refused before it ran).
NORMALIZATION_NONE = "none"

#: Checks a verdict needs: without them nothing establishes the outcome, so a
#: verdict is ``not_compared`` (never ``faithful``) when any is ``null``.
REQUIRED_CHECKS: tuple[str, ...] = ("exit_code", "stdout_normalized_sha256", "file_effects")

#: Required checks whose *absent* record (not an unusable one) leaves the verdict to
#: the other required checks: the check is ``null``, listed in ``unchecked``, and the
#: verdict says nothing about it. Added after the fact (ADR-0321 amendment), so a
#: capsule older than the record, a non-Python workload or a container/batch runner
#: is not turned ``not_compared`` by a check it could never have had.
UNCHECKED_WHEN_UNRECORDED: frozenset[str] = frozenset({"file_effects"})

#: Checks that are computed and reported but never decide the verdict.
ADVISORY_CHECKS: frozenset[str] = frozenset(
    {"stdout_sha256", "stderr_sha256", "file_writes_not_recorded"}
)

#: Largest stored stream read to normalise (capture keeps at most 64 MiB).
MAX_STREAM_BYTES = 128 * 1024 * 1024

#: Most differing files listed in the ``file_effects`` check; the rest are counted.
MAX_LISTED_FILE_DIFFERENCES = 50

#: Counters of calls the replay did not serve from the capsule. Any non-zero
#: value makes ``faithful`` impossible. ``model_calls_not_servable`` and
#: ``child_processes_unobserved`` are read when the replay contract carries them.
#: A child that reported in is not live by itself: its connections are in
#: ``network_connections_live`` (ADR-0319, #52 part 2); one that did not could
#: have reached anything.
LIVE_COUNTERS: tuple[str, ...] = (
    "model_calls_live",
    "tool_calls_live",
    "network_connections_live",
    "model_calls_not_servable",
    "child_processes_unobserved",
)

#: Divergence kinds meaning the workload never ran under the dispatchers.
_NOT_RUN_DIVERGENCES = frozenset({"dispatcher_install_failed"})

#: Stop reasons meaning the replayed command did not run to completion.
_NOT_COMPLETED_ERRORS = frozenset({"ReplayTimeout", "ReplayLaunchError"})

#: Most sequence entries listed per side in a check; the rest are counted.
MAX_LISTED_SEQUENCE = 50

#: How ``tool_call_sequence`` compared (its ``comparison`` field): the calls of
#: each model turn as a multiset, turns in order -- or, when the recording gives
#: no usable turn boundaries, the whole sequence in strict order.
COMPARISON_PER_TURN = "per-model-turn-multiset"
COMPARISON_STRICT = "strict-order"

STDOUT_REF = "outputs/stdout.txt"
STDERR_REF = "outputs/stderr.txt"

#: The only capture mode whose capsule records the process's output streams.
_OUTPUT_CAPTURE_MODE = "cli-wrapper"

#: Adapter capsules record the framework call's outcome, not the process's
#: (ADR-0306 Q7); the re-run adapter reports its own in ``adapter_run_end``.
_ADAPTER_CAPTURE_MODE = "sdk-decorator"


@dataclass
class FidelityCheck:
    """One comparison: ``equal`` is ``None`` when it could not be computed."""

    name: str
    equal: bool | None
    recorded: Any = None
    replayed: Any = None
    note: str | None = None
    #: The rule set the compared values went through (normalised checks only).
    normalizer: str | None = None
    #: Per-item differences (``file_effects``): ``{path, recorded, replayed}``.
    differences: list[dict[str, Any]] | None = None
    #: The capsule carries no record for this check at all (a capsule older than
    #: the check, or a capture that cannot produce it): the check is ``null`` but
    #: does not make the verdict ``not_compared``; the result lists it in
    #: ``unchecked``. Only for checks in :data:`UNCHECKED_WHEN_UNRECORDED`.
    unrecorded: bool = False
    #: How a sequence check compared (``tool_call_sequence`` only):
    #: :data:`COMPARISON_PER_TURN` or :data:`COMPARISON_STRICT`.
    comparison: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "equal": self.equal,
            "recorded": self.recorded,
            "replayed": self.replayed,
        }
        if self.note:
            out["note"] = self.note
        if self.normalizer:
            out["normalizer"] = self.normalizer
        if self.differences:
            out["differences"] = self.differences
        if self.comparison:
            out["comparison"] = self.comparison
        if self.name in ADVISORY_CHECKS:
            out["advisory"] = True
        return out


@dataclass
class Fidelity:
    """The ``fidelity`` block of a replay result."""

    verdict: str
    checks: list[FidelityCheck] = field(default_factory=list)
    reason: str | None = None

    @property
    def failing_checks(self) -> list[str]:
        return [
            c.name for c in self.checks if c.equal is False and c.name not in ADVISORY_CHECKS
        ]

    @property
    def advisory_differences(self) -> list[str]:
        return [c.name for c in self.checks if c.equal is False and c.name in ADVISORY_CHECKS]

    @property
    def uncompared_checks(self) -> list[str]:
        return [
            c.name for c in self.checks
            if c.equal is None and c.name in REQUIRED_CHECKS and not c.unrecorded
        ]

    @property
    def unchecked(self) -> list[str]:
        """Required checks the capsule holds no record for: the verdict does not cover them."""
        return [c.name for c in self.checks if c.equal is None and c.unrecorded]

    def as_dict(self) -> dict[str, Any]:
        normalizers = sorted({c.normalizer for c in self.checks if c.normalizer})
        out: dict[str, Any] = {
            "verdict": self.verdict,
            "normalization": normalizers[0] if normalizers else NORMALIZATION_NONE,
            "checks": [c.as_dict() for c in self.checks],
        }
        if self.unchecked:
            out["unchecked"] = self.unchecked
        if self.reason:
            out["reason"] = self.reason
        return out


def not_run(reason: str) -> Fidelity:
    """The verdict of a replay refused before its command ran."""
    return Fidelity(verdict=VERDICT_FAILED, reason=reason)


# ── recorded outputs ─────────────────────────────────────────────────────────


@dataclass
class StreamDigest:
    """A stored output stream's digest, or why there is none."""

    sha256: str | None
    note: str | None = None
    redacted: bool = False
    #: The digest after the volatile-token rule set (``_normalize.py``).
    normalized_sha256: str | None = None


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _digests(data: bytes, *, redacted: bool = False, note: str | None = None) -> StreamDigest:
    return StreamDigest(_sha(data), note, redacted, _sha(normalize_volatile(data)))


def _stored(capsule_dir: Path, rel: str) -> tuple[bytes | None, str | None, bool]:
    """``(bytes or None, problem, present)``; never follows a link."""
    try:
        data = read_required_capsule_file(
            capsule_dir, rel, operation="nova replay", max_bytes=MAX_STREAM_BYTES
        )
    except CapsuleRequiredFileError as exc:
        return None, f"{rel} is present but unusable: {exc}", True
    if data is None:
        return None, None, False
    return data, None, True


def recorded_stream(
    capsule_dir: Path, manifest: dict[str, Any], rel: str, proof: dict[str, Any] | None
) -> StreamDigest:
    """What the capsule recorded for one output stream.

    Only ``nova capture`` (``capture_mode: cli-wrapper``) records the process's
    output, and it writes ``outputs/<stream>.txt`` only when the stream was not
    empty. A capsule with ADR-0251 ``evidence_digests`` binds every file it
    holds, so an absent stream there *was* empty; in an older capsule an absent
    file cannot be told apart from an unrecorded one, and is not compared.
    """
    mode = manifest.get("capture_mode")
    if mode != _OUTPUT_CAPTURE_MODE:
        return StreamDigest(None, f"capture_mode {mode!r} does not record the process output")
    data, problem, present = _stored(capsule_dir, rel)
    digest = _sha(data) if data is not None else None
    if problem is not None:
        return StreamDigest(None, problem)
    evidence = manifest.get("evidence_digests")
    if isinstance(evidence, dict):
        entry = evidence.get(rel)
        bound = entry.get("sha256") if isinstance(entry, dict) else None
        if not present:
            if bound is not None:
                return StreamDigest(None, f"{rel} is listed in evidence_digests but missing")
            return _digests(
                b"",
                note=f"{rel} is absent from a capsule with evidence digests: the stream was empty",
            )
        if bound is not None and bound != digest:
            return StreamDigest(None, f"{rel} does not match its evidence digest")
    elif not present:
        return StreamDigest(
            None,
            f"{rel} is absent and the capsule predates ADR-0251 evidence digests, so "
            "an empty stream cannot be told apart from an unrecorded one",
        )
    masked = rel in _finding_refs(proof, "masker_findings")
    if masked:
        return StreamDigest(
            None,
            f"operator maskers (ADR-0135) changed {rel} at capture; replay cannot "
            "apply them, so the streams are not compared",
        )
    return _digests(data or b"", redacted=rel in _finding_refs(proof, "findings"))


def replayed_stream(
    replay_capsule: Path | None, rel: str, raw: bytes | None, proof: dict[str, Any] | None
) -> StreamDigest:
    """The replayed stream as the replay capsule stores it (after the same
    built-in secret scanner capture runs), or the raw bytes' digest when no
    replay capsule could be written."""
    if raw is None:
        return StreamDigest(None, "the replayed command did not run to completion")
    if replay_capsule is None:
        return _digests(raw)
    data, problem, present = _stored(replay_capsule, rel)
    if problem is not None:
        return StreamDigest(None, problem)
    if not present:
        # Written only when not empty, as `nova capture` writes it.
        if raw:
            return StreamDigest(None, f"{rel} was not written to the replay capsule")
        return _digests(b"")
    return _digests(data or b"", redacted=rel in _finding_refs(proof, "findings"))


def _finding_refs(proof: dict[str, Any] | None, key: str) -> set[str]:
    if not isinstance(proof, dict):
        return set()
    items = proof.get(key)
    if not isinstance(items, list):
        return set()
    return {str(f.get("target_ref")) for f in items if isinstance(f, dict)}


def _rules_hash(proof: dict[str, Any] | None) -> Any:
    packs = proof.get("packs") if isinstance(proof, dict) else None
    if isinstance(packs, list) and packs and isinstance(packs[0], dict):
        return packs[0].get("rules_hash")
    return None


def load_proof(capsule_dir: Path) -> dict[str, Any] | None:
    """``redaction-proof.json`` as a mapping, or ``None`` when absent/unreadable."""
    try:
        data = read_required_capsule_file(
            capsule_dir, "redaction-proof.json", operation="nova replay"
        )
    except CapsuleRequiredFileError:
        return None
    if data is None:
        return None
    try:
        proof = json.loads(data)
    except ValueError:
        return None
    return proof if isinstance(proof, dict) else None


def _stream_check(
    name: str,
    recorded: StreamDigest,
    replayed: StreamDigest,
    recorded_proof: dict[str, Any] | None,
    replayed_proof: dict[str, Any] | None,
    *,
    normalized: bool = False,
) -> FidelityCheck:
    normalizer = NORMALIZER_ID if normalized else None
    rec = recorded.normalized_sha256 if normalized else recorded.sha256
    rep = replayed.normalized_sha256 if normalized else replayed.sha256
    if rec is None:
        return FidelityCheck(name, None, None, rep, recorded.note, normalizer)
    if rep is None:
        return FidelityCheck(name, None, rec, None, replayed.note, normalizer)
    note = recorded.note
    if recorded.redacted or replayed.redacted:
        if _rules_hash(recorded_proof) != _rules_hash(replayed_proof):
            return FidelityCheck(
                name, None, rec, rep,
                "the secret scanner redacted this stream and its rule pack differs "
                "between capture and replay, so the stored streams are not comparable",
                normalizer,
            )
        note = (
            "compared as stored: the built-in secret scanner (same rule pack) redacted "
            + " and ".join(
                side for side, d in (("the recorded", recorded), ("the replayed", replayed))
                if d.redacted
            )
            + " stream"
        )
    return FidelityCheck(name, rec == rep, rec, rep, note, normalizer)


# ── file effects ─────────────────────────────────────────────────────────────


def _uncompared_files(note: str, *, unrecorded: bool = False) -> FidelityCheck:
    return FidelityCheck("file_effects", None, None, None, note, unrecorded=unrecorded)


class _NoFileEffectsRecord(Exception):
    """The capsule holds no file-effects record at all (not an unusable one)."""


def _recorded_file_effects(
    capsule_dir: Path, manifest: dict[str, Any], proof: dict[str, Any] | None
) -> tuple[list[FileState] | None, str | None]:
    """The recorded end-of-run file states, or ``(None, why they cannot be used)``.

    Raises :class:`_NoFileEffectsRecord` when there is no record to use at all.
    """
    mode = manifest.get("capture_mode")
    if mode != _OUTPUT_CAPTURE_MODE:
        raise _NoFileEffectsRecord(f"capture_mode {mode!r} does not record file effects")
    data, problem, present = _stored(capsule_dir, FILE_EFFECTS_REF)
    if problem is not None:
        return None, problem
    evidence = manifest.get("evidence_digests")
    entry = evidence.get(FILE_EFFECTS_REF) if isinstance(evidence, dict) else None
    bound = entry.get("sha256") if isinstance(entry, dict) else None
    if not present or data is None:
        if bound is not None:
            return None, f"{FILE_EFFECTS_REF} is listed in evidence_digests but missing"
        raise _NoFileEffectsRecord(
            f"file effects were not recorded for this capsule (no {FILE_EFFECTS_REF}: "
            "captured before file effects were recorded, a non-Python workload, or a "
            "container or batch runner), so the verdict does not cover files the run wrote"
        )
    if bound is not None and bound != _sha(data):
        return None, f"{FILE_EFFECTS_REF} does not match its evidence digest"
    if FILE_EFFECTS_REF in (
        _finding_refs(proof, "findings") | _finding_refs(proof, "masker_findings")
    ):
        return None, (
            f"redaction changed {FILE_EFFECTS_REF} at capture, so its paths cannot be "
            "resolved at replay"
        )
    try:
        record = parse_record(data)
    except InvalidFileEffectsError as exc:
        return None, str(exc)
    if not record.complete:
        return None, "the recorded file effects are incomplete: " + (record.reason or "unknown")
    return record.files, None


def file_effects_check(
    capsule_dir: Path,
    manifest: dict[str, Any],
    proof: dict[str, Any] | None,
    *,
    cwd: Path,
    home: Path,
    replay_writes: tuple[set[str] | None, str | None],
) -> FidelityCheck:
    """Each recorded written file **the replay wrote again**, digested now, against
    its recorded end state.

    *replay_writes*: the absolute paths the replayed processes opened for writing
    (``capture/file_effects.py:replay_written_paths``), or ``(None, why unknown)``.
    A recorded file the replay never touched is not judged -- a served tool's
    write is legitimately not repeated, and something else may have changed the
    file since the recording -- and is counted in the note. So are files only the
    replay wrote (a documented limit: they do not decide the verdict).

    ``equal`` is ``False`` when a re-written file differs (the paths are listed),
    ``None`` when nothing differs but a file could not be compared (too large,
    unreadable), the replay's writes are unknown, or the record is present but
    unusable (incomplete, redacted, not matching its evidence digest) -- the
    verdict is then ``not_compared``; ``True`` otherwise. No record at all:
    ``None`` with ``unrecorded`` set, so the verdict comes from the other
    required checks and lists it ``unchecked``.
    """
    try:
        recorded, problem = _recorded_file_effects(capsule_dir, manifest, proof)
    except _NoFileEffectsRecord as absent:
        return _uncompared_files(str(absent), unrecorded=True)
    if recorded is None:
        return _uncompared_files(problem or "no file effects recorded")
    written, unknown = replay_writes
    if written is None:
        if not recorded:
            written = set()  # nothing recorded to compare: the replay's writes do not matter
        else:
            return FidelityCheck(
                "file_effects", None, len(recorded), None,
                (unknown or "the replay's file writes are unknown")
                + "; the recorded files cannot be compared",
            )
    differences: list[dict[str, Any]] = []
    uncomparable: list[str] = []
    untouched = 0
    matched: set[str] = set()
    for rec in recorded:
        target = resolve_recorded_path(rec.path, cwd=cwd, home=home)
        key = os.path.abspath(target)
        if key not in written:
            untouched += 1
            continue
        matched.add(key)
        now = file_state(target, rec.path)
        if rec.state in UNCOMPARABLE_STATES or now.state in UNCOMPARABLE_STATES:
            uncomparable.append(rec.path)
        elif (rec.state, rec.sha256) != (now.state, now.sha256):
            differences.append(
                {"path": rec.path, "recorded": rec.describe(), "replayed": now.describe()}
            )
    total = len(recorded)
    rewritten = total - untouched
    new_paths = len(written - matched)
    summary = (
        f"{rewritten} of {total} recorded file(s) written again by the replay and compared; "
        f"{untouched} not touched by the replay (not judged); {new_paths} file(s) written "
        "only by the replay (not judged)"
    )
    listed = differences[:MAX_LISTED_FILE_DIFFERENCES]
    if differences:
        more = len(differences) - len(listed)
        note = (
            f"{len(differences)} recorded file(s) differ after the replay wrote them again: "
            + ", ".join(d["path"] for d in listed)
            + (f" (+{more} more)" if more else "")
            + f". {summary}"
        )
        return FidelityCheck("file_effects", False, total, rewritten, note, differences=listed)
    if uncomparable:
        return FidelityCheck(
            "file_effects", None, total, rewritten,
            f"{len(uncomparable)} recorded file(s) are too large or unreadable to compare: "
            + ", ".join(uncomparable[:MAX_LISTED_FILE_DIFFERENCES]) + f". {summary}",
        )
    return FidelityCheck("file_effects", True, total, rewritten, summary)


def replay_only_writes_check(
    capsule_dir: Path,
    manifest: dict[str, Any],
    proof: dict[str, Any] | None,
    *,
    cwd: Path,
    home: Path,
    replay_writes: tuple[set[str] | None, str | None],
) -> FidelityCheck:
    """Advisory: files the replayed process wrote that the recording never wrote
    (fidelity control S6b -- a report named after the run, say). Listed with their
    state now (first 50), never in the verdict: such paths are often volatile.
    ``null`` when the replay's writes or the recorded ones are unknown."""
    name = "file_writes_not_recorded"
    written, unknown = replay_writes
    if written is None:
        return FidelityCheck(name, None, 0, None, unknown)
    try:
        recorded, problem = _recorded_file_effects(capsule_dir, manifest, proof)
    except _NoFileEffectsRecord:
        recorded, problem = None, "file effects were not recorded for this capsule"
    if recorded is None:
        return FidelityCheck(
            name, None, 0, len(written), (problem or "no usable record")
            + "; the files the replay wrote cannot be told apart from recorded ones",
        )
    known = {
        os.path.abspath(resolve_recorded_path(r.path, cwd=cwd, home=home)) for r in recorded
    }
    new = sorted(
        p for p in written - known
        if "__pycache__" not in Path(p).parts and not p.endswith((".pyc", ".pyo"))
    )
    if not new:
        return FidelityCheck(name, True, 0, 0)
    listed = [
        {"path": shown, "recorded": "not written",
         "replayed": file_state(Path(p), shown).describe()}
        for p in new[:MAX_LISTED_FILE_DIFFERENCES]
        for shown in (display_path(p, cwd=cwd, home=home),)
    ]
    more = len(new) - len(listed)
    note = (
        f"{len(new)} file(s) written by the replay that the recording never wrote "
        "(advisory, not in the verdict): "
        + ", ".join(d["path"] for d in listed)
        + (f" (+{more} more)" if more else "")
    )
    return FidelityCheck(name, False, 0, len(new), note, differences=listed)


# ── call sequences ───────────────────────────────────────────────────────────


def _listed(seq: list[str]) -> list[str]:
    return seq[:MAX_LISTED_SEQUENCE]


def _sequence_check(name: str, recorded: list[str], replayed: list[str]) -> FidelityCheck:
    equal = recorded == replayed
    note = None
    if not equal:
        first = next(
            (i for i, (a, b) in enumerate(zip(recorded, replayed)) if a != b),
            min(len(recorded), len(replayed)),
        )
        note = (
            f"first difference at position {first + 1} "
            f"({len(recorded)} recorded, {len(replayed)} replayed)"
        )
    if len(recorded) > MAX_LISTED_SEQUENCE or len(replayed) > MAX_LISTED_SEQUENCE:
        note = (note + "; " if note else "") + f"first {MAX_LISTED_SEQUENCE} entries listed"
    return FidelityCheck(name, equal, _listed(recorded), _listed(replayed), note)


def _recorded_model_calls(
    model_calls: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[tuple[str, int, dict[str, Any]]]:
    """The servable records a replay is expected to request, in recorded order, as
    ``(queue, index in queue, record)``: every servable record less those a served
    nested ``record.tool`` boundary covered (never requested, ADR-0306 slice 4)."""
    queues = model_queues(model_calls)
    order: list[tuple[str, int]] = []
    seen: dict[str, int] = {}
    for served in served_model_records(model_calls):
        key = str(model_queue_key(served))
        order.append((key, seen.get(key, 0)))
        seen[key] = seen.get(key, 0) + 1
    covered = {
        (str(ev.get("queue")), int(ev.get("call_index") or 0))
        for ev in events if ev.get("event") == "model_covered"
    }
    return [(q, i, queues[q][i]) for q, i in order if (q, i) not in covered]


def _model_entry(queue: str, index: int, record: dict[str, Any] | None) -> str:
    surface = MODEL_SURFACES.get(queue, queue)
    call_id = record.get("model_call_id") if isinstance(record, dict) else None
    return f"{surface}#{index + 1}:{call_id}"


def model_sequences(
    model_calls: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Recorded and replayed model-call sequences, as ``surface#n:model_call_id``.

    Recorded: every servable record in recorded order, less the records a served
    nested ``record.tool`` boundary covered (never requested, ADR-0306 slice 4).
    Replayed: the records the dispatcher served, in the order it served them, and
    ``live:<surface>`` for each call that went live.
    """
    queues = model_queues(model_calls)
    recorded = [
        _model_entry(q, i, record) for q, i, record in _recorded_model_calls(model_calls, events)
    ]
    replayed: list[str] = []
    for ev in events:
        kind = ev.get("event")
        if kind == "model_served":
            queue = str(ev.get("queue") or ev.get("provider"))
            index = int(ev.get("call_index") or 0)
            records = queues.get(queue) or []
            replayed.append(
                _model_entry(queue, index, records[index] if 0 <= index < len(records) else None)
            )
        elif kind == "model_live":
            replayed.append(f"live:{ev.get('surface') or ev.get('provider')}")
    return recorded, replayed


def _tool_entry(surface: Any, name: Any, ident: Any) -> str:
    return f"{surface or TOOL_SURFACE_MCP}:{name}:{ident}"


def _recorded_tool_calls(
    tool_calls: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    """``(entry, record)`` for each intercepted recorded tool call the replay is
    expected to make, in recorded order (less those a served boundary covered)."""
    covered = {
        ev.get("record_id") for ev in events if ev.get("event") == "tool_covered"
    }
    return [
        (_tool_entry(tool_surface(r), r.get("tool_name"), r.get("tool_call_id")), r)
        for r in interceptable_tool_calls(tool_calls)
        if r.get("tool_call_id") is None or r.get("tool_call_id") not in covered
    ]


def _replayed_tool_calls(events: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """``(entry, model turn)`` for each tool call the replay made, in event order.

    The turn is the number of model calls (served or live) the replay made before
    the tool call: 0 before the first model call, *k* after the *k*-th."""
    out: list[tuple[str, int]] = []
    turn = 0
    # A divergence then a live run (or refusal) of the same call is one call.
    for ev in collapse_tool_divergences(events):
        kind = ev.get("event")
        if kind in ("model_served", "model_live"):
            turn += 1
        elif kind == "tool_mocked":
            out.append(
                (_tool_entry(ev.get("surface"), ev.get("tool_name"), ev.get("record_id")), turn)
            )
        elif kind in ("tool_live", "tool_refused"):
            out.append((
                _tool_entry(ev.get("surface"), ev.get("tool_name"), kind.removeprefix("tool_")),
                turn,
            ))
        elif kind == "divergence" and ev.get("kind") in (
            "tool_call_unmatched", "tool_result_not_servable",
        ):
            out.append((_tool_entry(ev.get("surface"), ev.get("tool_name"), "unmatched"), turn))
    return out


def tool_sequences(
    tool_calls: list[dict[str, Any]], events: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Recorded and replayed intercepted tool-call sequences, as
    ``surface:tool_name:tool_call_id``. A replayed call that was not served is
    ``...:live``, ``...:refused`` or ``...:unmatched``."""
    return (
        [entry for entry, _ in _recorded_tool_calls(tool_calls, events)],
        [entry for entry, _ in _replayed_tool_calls(events)],
    )


class TurnBoundariesUnavailable(ValueError):
    """The recording does not say which model turn each tool call belongs to."""


def _started_at(record: dict[str, Any], what: str) -> datetime:
    value = record.get("started_at")
    if not isinstance(value, str):
        raise TurnBoundariesUnavailable(f"a recorded {what} has no started_at")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise TurnBoundariesUnavailable(
            f"a recorded {what} has an unreadable started_at"
        ) from exc
    if stamp.tzinfo is None:
        raise TurnBoundariesUnavailable(f"a recorded {what} started_at has no time zone")
    return stamp


def recorded_tool_turns(
    model_calls: list[dict[str, Any]],
    tool_records: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[int]:
    """The model turn of each recorded tool call: how many of the model calls the
    replay is expected to make had started before it (``started_at``).

    Raises :class:`TurnBoundariesUnavailable` when a timestamp is missing or
    unreadable, or the recorded model calls did not start in recorded order
    (concurrent agents): turns are then unknown, never guessed."""
    boundaries = [
        _started_at(record, "model call")
        for _, _, record in _recorded_model_calls(model_calls, events)
    ]
    if any(later < earlier for earlier, later in zip(boundaries, boundaries[1:])):
        raise TurnBoundariesUnavailable("the recorded model calls did not start in time order")
    return [bisect_right(boundaries, _started_at(r, "tool call")) for r in tool_records]


def _by_turn(entries: list[tuple[str, int]]) -> dict[int, Counter[str]]:
    turns: dict[int, Counter[str]] = {}
    for entry, turn in entries:
        turns.setdefault(turn, Counter())[entry] += 1
    return turns


def _turn_order(entries: list[tuple[str, int]]) -> dict[int, list[str]]:
    turns: dict[int, list[str]] = {}
    for entry, turn in entries:
        turns.setdefault(turn, []).append(entry)
    return turns


def tool_sequence_check(
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> FidelityCheck:
    """``tool_call_sequence``: the same tool calls in the same model turns.

    Within one model turn -- the tool calls between two consecutive model calls,
    which answer the same model response -- the calls are compared as a multiset:
    parallel calls may complete, and be served, in another order. Across turns the
    order is strict. Without usable turn boundaries
    (:class:`TurnBoundariesUnavailable`) the whole sequence is compared in strict
    order, as before."""
    recorded_pairs = _recorded_tool_calls(tool_calls, events)
    replayed_turns = _replayed_tool_calls(events)
    recorded = [entry for entry, _ in recorded_pairs]
    replayed = [entry for entry, _ in replayed_turns]
    try:
        turns = recorded_tool_turns(model_calls, [r for _, r in recorded_pairs], events)
    except TurnBoundariesUnavailable as exc:
        check = _sequence_check("tool_call_sequence", recorded, replayed)
        check.comparison = COMPARISON_STRICT
        check.note = (check.note + "; " if check.note else "") + (
            f"turn boundaries unavailable ({exc}): compared in strict order"
        )
        return check
    recorded_turns = list(zip(recorded, turns, strict=True))
    want, got = _by_turn(recorded_turns), _by_turn(replayed_turns)
    differing = sorted(t for t in set(want) | set(got) if want.get(t) != got.get(t))
    note: str | None = None
    if differing:
        first = differing[0]
        note = (
            f"first difference in model turn {first} "
            f"({sum(want.get(first, Counter()).values())} recorded, "
            f"{sum(got.get(first, Counter()).values())} replayed in that turn; "
            f"{len(recorded)} recorded, {len(replayed)} replayed in all)"
        )
    else:
        want_order, got_order = _turn_order(recorded_turns), _turn_order(replayed_turns)
        reordered = sorted(t for t in want_order if want_order[t] != got_order.get(t))
        if reordered:
            note = (
                f"order within model turn {', '.join(str(t) for t in reordered)} differed "
                "(calls in one turn are compared as a multiset: parallel tool calls)"
            )
    if len(recorded) > MAX_LISTED_SEQUENCE or len(replayed) > MAX_LISTED_SEQUENCE:
        note = (note + "; " if note else "") + f"first {MAX_LISTED_SEQUENCE} entries listed"
    return FidelityCheck(
        "tool_call_sequence", not differing, _listed(recorded), _listed(replayed), note,
        comparison=COMPARISON_PER_TURN,
    )


def not_interceptable_tool_count(tool_calls: list[dict[str, Any]]) -> int:
    """Recorded tool calls on a surface replay does not intercept: at replay they
    re-run live, unobserved. A proxy duplicate of a hook record is not counted."""
    return sum(1 for r in tool_calls if not is_interceptable_tool_call(r))


# ── verdict ──────────────────────────────────────────────────────────────────


def _live_check(
    report: ReplayContractReport, contract: dict[str, Any], tool_calls: list[dict[str, Any]]
) -> FidelityCheck:
    counts: dict[str, int] = {}
    for name in LIVE_COUNTERS:
        value = contract.get(name, getattr(report, name, 0))
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value:
            counts[name] = value
    not_interceptable = not_interceptable_tool_count(tool_calls)
    if not_interceptable:
        counts["tool_calls_not_interceptable"] = not_interceptable
    note = None
    if counts:
        note = (
            "the replay made or could not intercept calls the capsule answers: "
            + ", ".join(f"{k}={v}" for k, v in counts.items())
        )
    return FidelityCheck("live_calls", not counts, 0, sum(counts.values()), note)


def _echo_check(report: ReplayContractReport) -> FidelityCheck:
    checked = report.echo_matched + len(report.echo_mismatches)
    if not checked:
        return FidelityCheck(
            "tool_result_echo", None, None, None,
            "no undeclared tool result was echoed to a model (ADR-0306 D10)",
        )
    return FidelityCheck(
        "tool_result_echo", not report.echo_mismatches, checked, report.echo_matched,
        None if not report.echo_mismatches
        else f"{len(report.echo_mismatches)} tool result(s) differ from capture",
    )


def _adapter_outcome_check(
    recorded_exit: int, events: list[dict[str, Any]], process_exit: int | None
) -> FidelityCheck:
    """The ``exit_code`` check of an adapter capsule re-run from its recorded
    process invocation: the first adapter run's outcome against the one the
    capsule recorded -- never the process's exit code, which it did not record."""
    ends = [
        ev for ev in events
        if ev.get("event") == "adapter_run_end" and ev.get("ordinal") == 1
    ]
    replayed = ends[0].get("exit_code") if ends else None
    if isinstance(replayed, bool) or not isinstance(replayed, int):
        return FidelityCheck(
            "exit_code", None, recorded_exit, None,
            "an adapter capsule records the outcome of its framework call, not the "
            "process exit code, and the replayed process reported no finished framework "
            f"call (the process exited {process_exit})",
        )
    return FidelityCheck(
        "exit_code", recorded_exit == replayed, recorded_exit, replayed,
        "the framework call's outcome: an adapter capsule records it, not the process "
        f"exit code (the replayed process exited {process_exit})",
    )


def compute_fidelity(
    *,
    capsule_dir: Path,
    manifest: dict[str, Any],
    model_calls: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    events: list[dict[str, Any]],
    report: ReplayContractReport,
    exit_code: int | None,
    run_error: dict[str, Any] | None,
    stdout: bytes | None,
    stderr: bytes | None,
    replay_capsule: Path | None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Fidelity:
    """The fidelity verdict of one mocked replay (ADR-0321).

    ``failed``: the replayed command did not run to completion (timeout, launch
    error, dispatcher not installed). ``diverged``: any computed check is
    unequal. ``not_compared``: a required check (exit code, normalised stdout,
    file effects) could not be computed -- except a check whose record the
    capsule never had (:data:`UNCHECKED_WHEN_UNRECORDED`), which is listed in
    ``unchecked`` instead. ``faithful``: none of these.

    *cwd* / *home*: the directories the replayed process ran in, against which
    recorded file paths resolve (default: this process's).
    """
    completed = run_error is None or run_error.get("type") not in _NOT_COMPLETED_ERRORS
    recorded_exit = manifest.get("exit_code")
    if isinstance(recorded_exit, bool) or not isinstance(recorded_exit, int):
        exit_check = FidelityCheck(
            "exit_code", None, None, exit_code if completed else None,
            "capsule.yaml records no exit_code",
        )
    elif not completed:
        exit_check = FidelityCheck(
            "exit_code", None, recorded_exit, None,
            f"the replayed command did not exit on its own ({(run_error or {}).get('type')})",
        )
    elif manifest.get("capture_mode") == _ADAPTER_CAPTURE_MODE:
        exit_check = _adapter_outcome_check(recorded_exit, events, exit_code)
    else:
        exit_check = FidelityCheck(
            "exit_code", recorded_exit == exit_code, recorded_exit, exit_code
        )

    recorded_proof = load_proof(capsule_dir)
    replayed_proof = load_proof(replay_capsule) if replay_capsule is not None else None
    checks = [exit_check]
    for rel, raw, names in (
        (STDOUT_REF, stdout, (("stdout_normalized_sha256", True), ("stdout_sha256", False))),
        (STDERR_REF, stderr, (("stderr_sha256", False),)),
    ):
        recorded = recorded_stream(capsule_dir, manifest, rel, recorded_proof)
        replayed = replayed_stream(
            replay_capsule, rel, raw if completed else None, replayed_proof
        )
        for name, normalized in names:
            checks.append(_stream_check(
                name, recorded, replayed, recorded_proof, replayed_proof,
                normalized=normalized,
            ))
    if completed:
        run_cwd = cwd if cwd is not None else Path.cwd()
        run_home = home if home is not None else Path.home()
        writes = replay_written_paths(events)
        for check_fn in (file_effects_check, replay_only_writes_check):
            checks.append(check_fn(
                capsule_dir, manifest, recorded_proof,
                cwd=run_cwd, home=run_home, replay_writes=writes,
            ))
    else:
        checks.append(_uncompared_files("the replayed command did not run to completion"))

    checks.append(_sequence_check("model_call_sequence", *model_sequences(model_calls, events)))
    checks.append(tool_sequence_check(model_calls, tool_calls, events))
    checks.append(FidelityCheck(
        "divergence", not report.diverged, 0, len(report.divergences), report.divergence_reason
    ))
    checks.append(_live_check(report, report.as_dict(), tool_calls))
    checks.append(_echo_check(report))

    not_run_kinds = {str(d.get("kind")) for d in report.divergences} & _NOT_RUN_DIVERGENCES
    if not completed or not_run_kinds:
        reason = (
            (run_error or {}).get("message")
            if not completed
            else "the replay dispatcher could not be installed in the replayed process"
        )
        return Fidelity(verdict=VERDICT_FAILED, checks=checks, reason=reason)
    fidelity = Fidelity(verdict=VERDICT_FAITHFUL, checks=checks)
    if fidelity.failing_checks:
        fidelity.verdict = VERDICT_DIVERGED
    elif fidelity.uncompared_checks:
        fidelity.verdict = VERDICT_NOT_COMPARED
    return fidelity
