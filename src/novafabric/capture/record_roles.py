"""Logical vs transport model-call records (ADR-0305).

An OpenAI/Anthropic SDK call is recorded twice in ``model-calls.jsonl``: once
by the SDK hook (the *logical* call, with the parsed response) and once per
HTTP attempt by the wire hook underneath it (``httpx``; written first, no
response). Both are kept -- the wire records are evidence of retries and of
what went over the wire -- but only the logical record is a model call.

Marker (additive, optional, in the record's ``extensions``):

* ``io.novafabric.record_role``: ``"logical"`` | ``"transport"``.
* ``io.novafabric.logical_call_id``: the ``model_call_id`` of the logical call
  the record belongs to. On a logical record it is its own ``model_call_id``;
  on a transport record it is the covering SDK record's id, so every retry of
  one SDK call maps to one logical call.

A record without the marker is logical, except in a capsule written before
ADR-0305 (no record carries the marker), where :func:`classify_model_calls`
recognises the old duplicate shape -- see :func:`_legacy_transport_links`.

Consumers that count or iterate model calls use :func:`logical_model_calls`
(or :func:`count_logical_model_calls_in_file`); evidence surfaces that hash,
scan, redact or bundle the file keep reading every record.

Capture side: the SDK hooks wrap the SDK call in :func:`sdk_call_scope`; the
wire hooks call :func:`stamp_wire_record`, which marks the record transport
while a scope is active (retries all land inside it) and logical otherwise
(raw ``httpx``, non-SDK clients). A ``ContextVar`` follows the call through
``asyncio`` tasks, so async SDK calls are covered too.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

#: Reverse-DNS extension key naming the record's role.
RECORD_ROLE_EXT = "io.novafabric.record_role"
#: Reverse-DNS extension key linking a record to its logical call.
LOGICAL_CALL_ID_EXT = "io.novafabric.logical_call_id"

ROLE_LOGICAL = "logical"
ROLE_TRANSPORT = "transport"

#: Extension key the SDK hooks write on their records (ADR-0304). Duplicated
#: here (not imported from the hooks package) to keep this module import-light.
_API_SURFACE_EXT = "io.novafabric.api_surface"

_T = TypeVar("_T")

# The logical call id of the SDK call currently in progress, or None.
_current_sdk_call: ContextVar[str | None] = ContextVar(
    "novafabric_current_sdk_call", default=None
)


# ── capture side ─────────────────────────────────────────────────────────────


@contextmanager
def sdk_call_scope(logical_call_id: str) -> Iterator[str]:
    """Mark the enclosed code as one logical SDK call.

    Every wire record written inside the scope becomes a transport record of
    ``logical_call_id``. Restored on exit (including on exceptions), so nested
    and concurrent calls do not leak into each other.
    """
    token = _current_sdk_call.set(logical_call_id)
    try:
        yield logical_call_id
    finally:
        _current_sdk_call.reset(token)


def current_sdk_call_id() -> str | None:
    """The logical call id of the enclosing SDK call, or None outside one."""
    return _current_sdk_call.get()


def _extensions(record: dict[str, Any]) -> dict[str, Any]:
    ext = record.get("extensions")
    if not isinstance(ext, dict):
        ext = {}
        record["extensions"] = ext
    return ext


def stamp_logical_record(record: dict[str, Any]) -> dict[str, Any]:
    """Mark ``record`` as a logical model call (links to itself). Returns it."""
    ext = _extensions(record)
    ext[RECORD_ROLE_EXT] = ROLE_LOGICAL
    ext[LOGICAL_CALL_ID_EXT] = str(record.get("model_call_id") or "")
    return record


def stamp_wire_record(record: dict[str, Any]) -> dict[str, Any]:
    """Mark a wire-hook record: transport inside an SDK call, logical outside.

    Never raises into the caller (capture must not fail the workload).
    """
    try:
        logical = current_sdk_call_id()
        if logical is None:
            return stamp_logical_record(record)
        ext = _extensions(record)
        ext[RECORD_ROLE_EXT] = ROLE_TRANSPORT
        ext[LOGICAL_CALL_ID_EXT] = logical
    except Exception:  # noqa: BLE001 -- capture must never fail the workload
        pass
    return record


# ── read side ────────────────────────────────────────────────────────────────


def _ext(record: Mapping[str, Any]) -> Mapping[str, Any]:
    ext = record.get("extensions")
    return ext if isinstance(ext, Mapping) else {}


def marked_role(record: Any) -> str | None:
    """The role the record's marker states, or None when it carries none."""
    if not isinstance(record, Mapping):
        return None
    role = _ext(record).get(RECORD_ROLE_EXT)
    return role if role in (ROLE_LOGICAL, ROLE_TRANSPORT) else None


def is_transport_record(record: Any) -> bool:
    """True when the record's own marker says it is transport.

    Marker-only: for a whole capsule (including the pre-ADR-0305 fallback and
    orphaned transport records) use :func:`logical_model_calls`.
    """
    return marked_role(record) == ROLE_TRANSPORT


@dataclass(frozen=True)
class RecordRole:
    """How one ``model-calls.jsonl`` record counts.

    ``counts`` is True for exactly one record per logical call. A transport
    record whose logical record is absent (the SDK record was never written,
    e.g. the call was cancelled) is *promoted*: its last attempt counts, so a
    call that left only wire evidence is never dropped from a count.
    """

    role: str
    logical_call_id: str | None
    counts: bool
    inferred: bool = False


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _request_key(record: Mapping[str, Any]) -> str:
    return json.dumps(
        [record.get("gen_ai.request.model"), record.get("gen_ai.request.messages")],
        sort_keys=True,
        default=str,
    )


def _is_wire_shaped(record: Mapping[str, Any]) -> bool:
    """The pre-ADR-0305 wire record: no response, no SDK markers, no error."""
    ext = _ext(record)
    if _API_SURFACE_EXT in ext:
        return False
    if record.get("gen_ai.response.choices"):
        return False
    return not any(
        k in record
        for k in ("gen_ai.response.id", "error", "nova.streaming",
                  "gen_ai.response.finish_reasons")
    )


def _contains(outer: Mapping[str, Any], inner: Mapping[str, Any]) -> bool | None:
    """True/False when both records carry parseable timestamps, else None."""
    o_start, o_end = _parse_ts(outer.get("started_at")), _parse_ts(outer.get("finished_at"))
    i_start, i_end = _parse_ts(inner.get("started_at")), _parse_ts(inner.get("finished_at"))
    if None in (o_start, o_end, i_start, i_end):
        return None
    try:
        return o_start <= i_start and i_end <= o_end  # type: ignore[operator]
    except TypeError:  # naive vs aware
        return None


def _legacy_transport_links(records: Sequence[Any]) -> dict[int, str]:
    """Pre-ADR-0305 capsules: index of each wire duplicate -> its SDK record id.

    A record is a wire duplicate when it is wire-shaped (no response, no SDK
    surface marker, no error block) and a LATER record that is not wire-shaped
    carries the same request (model + messages) and either encloses it in time
    (both timestamps parse) or -- without timestamps -- follows it with only
    wire-shaped copies of the same request in between (retries). Anything less
    certain is left logical: the fallback never under-counts.
    """
    n = len(records)
    wire = [isinstance(r, Mapping) and _is_wire_shaped(r) for r in records]
    keys = [_request_key(r) if isinstance(r, Mapping) else None for r in records]
    # Candidate SDK records per request key, in file order.
    groups: dict[str, list[int]] = {}
    for j, r in enumerate(records):
        if isinstance(r, Mapping) and not wire[j]:
            groups.setdefault(str(keys[j]), []).append(j)
    # brk[i]: first index after i that is NOT a wire-shaped copy of i's request.
    brk = [n] * n
    for i in range(n - 2, -1, -1):
        same = wire[i + 1] and keys[i + 1] == keys[i]
        brk[i] = brk[i + 1] if same else i + 1

    links: dict[int, str] = {}
    for i, rec in enumerate(records):
        if not wire[i]:
            continue
        cands = groups.get(str(keys[i]), [])
        for j in cands[bisect_right(cands, i):]:
            enclosed = _contains(records[j], rec)
            if enclosed is False:
                continue
            if enclosed is None and j > brk[i]:
                break
            cid = records[j].get("model_call_id")
            if isinstance(cid, str) and cid:
                links[i] = cid
            break
    return links


def classify_model_calls(records: Sequence[Any]) -> list[RecordRole]:
    """The role of every record, in order (see :class:`RecordRole`).

    A capsule in which any record carries the role marker is classified by
    markers alone (unmarked records there are logical: adapter, proxy and
    OTel-ingest records). A capsule with no marker at all predates ADR-0305 and
    goes through :func:`_legacy_transport_links`.
    """
    marked = any(marked_role(r) is not None for r in records)
    links: dict[int, str]
    if marked:
        links = {}
        for i, r in enumerate(records):
            if marked_role(r) == ROLE_TRANSPORT:
                links[i] = str(_ext(r).get(LOGICAL_CALL_ID_EXT) or "")
    else:
        links = _legacy_transport_links(records)

    present: set[str] = set()
    for i, r in enumerate(records):
        if i not in links and isinstance(r, Mapping):
            cid = r.get("model_call_id")
            if isinstance(cid, str) and cid:
                present.add(cid)
    # Orphaned transport records: promote the LAST attempt of each logical id.
    last_orphan: dict[str, int] = {}
    for i, target in links.items():
        if target not in present:
            last_orphan[target] = i

    roles: list[RecordRole] = []
    for i, r in enumerate(records):
        if i in links:
            target = links[i]
            roles.append(RecordRole(
                ROLE_TRANSPORT, target or None,
                counts=last_orphan.get(target) == i, inferred=not marked,
            ))
            continue
        cid = r.get("model_call_id") if isinstance(r, Mapping) else None
        linked = _ext(r).get(LOGICAL_CALL_ID_EXT) if isinstance(r, Mapping) else None
        roles.append(RecordRole(
            ROLE_LOGICAL,
            str(linked or cid) if (linked or cid) else None,
            counts=True,
        ))
    return roles


def logical_model_calls(records: Iterable[_T]) -> list[_T]:
    """The records that are model calls, in recorded order (one per call)."""
    items = list(records)
    return [r for r, role in zip(items, classify_model_calls(items)) if role.counts]


def count_logical_model_calls(records: Iterable[Any]) -> int:
    """Number of logical model calls among ``records``."""
    return sum(1 for role in classify_model_calls(list(records)) if role.counts)


def _read_lines(path: Path) -> list[Any]:
    """Every non-blank line, parsed; a malformed line is kept as the raw string."""
    out: list[Any] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            out.append(line)
    return out


def read_logical_model_calls(path: Path) -> list[dict[str, Any]]:
    """Logical model-call records of a ``model-calls.jsonl`` (malformed lines skipped)."""
    return [r for r in logical_model_calls(_read_lines(path)) if isinstance(r, dict)]


def count_logical_model_calls_in_file(path: Path) -> int:
    """``model_call_count`` for a ``model-calls.jsonl``.

    A malformed non-blank line still counts as one call (the pre-ADR-0305
    line count did), so the count never drops evidence it cannot parse.
    Missing file -> 0.
    """
    return count_logical_model_calls(_read_lines(path))


def split_model_calls(records: Iterable[_T]) -> tuple[list[_T], list[_T]]:
    """``(logical, transport)``: every record lands in exactly one list."""
    items = list(records)
    logical: list[_T] = []
    transport: list[_T] = []
    for r, role in zip(items, classify_model_calls(items)):
        (logical if role.counts else transport).append(r)
    return logical, transport


def non_counting_model_call_ids(path: Path) -> set[str]:
    """``model_call_id`` of every record in ``path`` that is not a model call.

    For line-by-line consumers (knowledge-graph ingest) that cannot hold the
    whole file in their own loop: skip a parsed record whose id is in the set.
    """
    items = _read_lines(path)
    out: set[str] = set()
    for r, role in zip(items, classify_model_calls(items)):
        if not role.counts and isinstance(r, Mapping):
            cid = r.get("model_call_id")
            if isinstance(cid, str):
                out.add(cid)
    return out
