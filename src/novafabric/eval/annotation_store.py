# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Local annotation-queue store + workflow transitions (ADR-0118 D5) — SQLite,
local-first.

Stores :class:`~novafabric.eval.annotation_queue.AnnotationQueue` and
:class:`~novafabric.eval.annotation_queue.QueueItem` records in the registry's
own SQLite database (``registry.store.get_connection``), in two **additive**
tables — mirroring the score-config catalog's storage decision
(:mod:`novafabric.eval.score_config_catalog`): no new storage backend, no
change to the shared ``assets`` schema, no server, no internet.

Workflow invariants (spec ``the private design/spec/annotation-queue-v0.md``, normative):

* **Atomic claim** — ``pending → assigned`` is a single state-guarded
  ``UPDATE``; if two reviewers race, exactly one wins.
* **No partial write** — ``submit`` validates every criterion against its
  ADR-0117 score config *before* any ``scores.jsonl`` append; a rejection
  appends nothing and the item stays ``assigned`` (retryable).
* **Evidence reuse** — completion writes ordinary ``HUMAN``-source ``Score``
  records via the existing :func:`novafabric.eval.scores.append_score`; the
  queue never mutates ``scores.jsonl`` other than appending.
* **Signed workflow evidence** — the maker's submission and the checker's
  confirmation are Ed25519-signed with the existing ADR-0058 keyring
  (:mod:`novafabric.trust.keyring`); fingerprints + signatures are carried in
  the item's ``extensions`` (reverse-DNS keys), never on a ``Score``.
* **Separation of duties** — a checker whose identity *or* key fingerprint
  equals the maker's is refused (ADR-0118 D4 / ADR-0003).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from novafabric.eval.annotation_queue import (
    AnnotationError,
    AnnotationQueue,
    AssignmentPolicy,
    CriteriaError,
    ItemNotFoundError,
    ItemState,
    ItemStateError,
    QueueExistsError,
    QueueItem,
    QueueNotFoundError,
    SeparationOfDutiesError,
    SubjectMismatchError,
    SubjectSelector,
    confirmation_payload,
    submission_payload,
)
from novafabric.eval.score_config import ScoreConfig, validate_score_against_config
from novafabric.eval.score_config_catalog import find_config_for_score
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    append_score,
)
from novafabric.registry.store import get_connection
from novafabric.trust.keyring import ensure_keypair, sign_payload

__all__ = [
    "annotation_subject_digest",
    "claim_item",
    "claim_next",
    "confirm_item",
    "create_queue",
    "enqueue_item",
    "get_item",
    "get_queue",
    "list_items",
    "list_queues",
    "queue_progress",
    "skip_item",
    "submit_item",
]

#: Reverse-DNS extension keys carrying the Ed25519 workflow evidence (D4).
_EXT_MAKER_FP = "io.novafabric.annotation.maker_key_fp"
_EXT_MAKER_SIG = "io.novafabric.annotation.maker_signature"
_EXT_SUBMITTED_AT = "io.novafabric.annotation.submitted_at"
_EXT_CHECKER_FP = "io.novafabric.annotation.checker_key_fp"
_EXT_CHECKER_SIG = "io.novafabric.annotation.checker_signature"
_EXT_CONFIRMED_AT = "io.novafabric.annotation.confirmed_at"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS annotation_queues (
            queue_id    TEXT PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            queue_json  TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS annotation_items (
            item_id     TEXT PRIMARY KEY,
            queue_id    TEXT NOT NULL,
            state       TEXT NOT NULL,
            item_json   TEXT NOT NULL,
            FOREIGN KEY(queue_id) REFERENCES annotation_queues(queue_id)
        );
        CREATE INDEX IF NOT EXISTS idx_annotation_items_queue_state
            ON annotation_items(queue_id, state);
        """
    )
    conn.commit()


def _row_to_queue(row: sqlite3.Row) -> AnnotationQueue:
    return AnnotationQueue.model_validate_json(row["queue_json"])


def _row_to_item(row: sqlite3.Row) -> QueueItem:
    return QueueItem.model_validate_json(row["item_json"])


def _get_queue(conn: sqlite3.Connection, ref: str) -> AnnotationQueue:
    row = conn.execute(
        "SELECT queue_json FROM annotation_queues WHERE queue_id = ? OR name = ?",
        (ref, ref),
    ).fetchone()
    if row is None:
        raise QueueNotFoundError(f"no annotation queue matches {ref!r} (id or name)")
    return _row_to_queue(row)


def _get_item(conn: sqlite3.Connection, item_id: str) -> QueueItem:
    row = conn.execute(
        "SELECT item_json FROM annotation_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise ItemNotFoundError(f"no queue item matches {item_id!r}")
    return _row_to_item(row)


def _update_item(
    conn: sqlite3.Connection, item: QueueItem, *, expect_state: ItemState
) -> bool:
    """State-guarded item update — the atomic transition primitive.

    Returns ``False`` when the row was not in *expect_state* anymore (a
    concurrent transition won); the caller decides whether that is a race
    (claim) or an error (submit/confirm).
    """
    cur = conn.execute(
        "UPDATE annotation_items SET state = ?, item_json = ? "
        "WHERE item_id = ? AND state = ?",
        (
            item.state.value,
            item.model_dump_json(exclude_none=True),
            item.item_id,
            expect_state.value,
        ),
    )
    conn.commit()
    return cur.rowcount == 1


# ── Queues ─────────────────────────────────────────────────────────────────────


def create_queue(
    name: str,
    criteria: list[str],
    assignment_policy: AssignmentPolicy = AssignmentPolicy.ROUND_ROBIN,
    subject_selector: SubjectSelector | None = None,
    require_checker: bool = False,
    seal: bool = False,
    description: str | None = None,
    db_path: Path | None = None,
) -> AnnotationQueue:
    """Create a named queue; every criterion must name a registered score config.

    Requiring the ADR-0117 config up front keeps the queue reproducible: the
    metric a reviewer will grade is content-addressed from day one.
    """
    queue = AnnotationQueue(
        name=name,
        criteria=criteria,
        assignment_policy=assignment_policy,
        subject_selector=subject_selector or SubjectSelector(),
        require_checker=require_checker,
        seal=seal,
        description=description,
    )
    for criterion in queue.criteria:
        if find_config_for_score(criterion, db_path=db_path) is None:
            raise CriteriaError(
                f"criterion {criterion!r} has no registered score config — register it "
                f"first with 'nova eval score config add --name {criterion} ...' (ADR-0117)"
            )
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        try:
            conn.execute(
                "INSERT INTO annotation_queues (queue_id, name, queue_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    queue.queue_id,
                    queue.name,
                    queue.model_dump_json(exclude_none=True),
                    queue.created_at,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise QueueExistsError(
                f"an annotation queue named {name!r} already exists"
            ) from exc
        return queue
    finally:
        conn.close()


def get_queue(ref: str, db_path: Path | None = None) -> AnnotationQueue:
    """Load a queue by ``queue_id`` (ULID) or unique ``name``."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        return _get_queue(conn, ref)
    finally:
        conn.close()


def list_queues(db_path: Path | None = None) -> list[AnnotationQueue]:
    """List every queue, ordered by name."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            "SELECT queue_json FROM annotation_queues ORDER BY name"
        ).fetchall()
        return [_row_to_queue(r) for r in rows]
    finally:
        conn.close()


def queue_progress(ref: str, db_path: Path | None = None) -> dict[str, int]:
    """Item counts per state for one queue (every state present, zero-filled)."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        queue = _get_queue(conn, ref)
        counts = {state.value: 0 for state in ItemState}
        for row in conn.execute(
            "SELECT state, COUNT(*) AS n FROM annotation_items "
            "WHERE queue_id = ? GROUP BY state",
            (queue.queue_id,),
        ):
            counts[row["state"]] = row["n"]
        return counts
    finally:
        conn.close()


# ── Items ──────────────────────────────────────────────────────────────────────


def enqueue_item(
    queue_ref: str,
    subject: str,
    subject_kind: str,
    capsule_ref: str | None = None,
    db_path: Path | None = None,
) -> QueueItem:
    """Add one subject to a queue as a ``pending`` item.

    ``capsule_ref`` names the capsule directory whose ``scores.jsonl`` will
    receive the completed scores. The queue's ``subject_selector`` acts as a
    guard: an explicit ``subject_kind`` restriction refuses mismatched subjects.
    """
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        queue = _get_queue(conn, queue_ref)
        selector_kind = queue.subject_selector.subject_kind
        if selector_kind is not None and selector_kind != subject_kind:
            raise SubjectMismatchError(
                f"queue {queue.name!r} only accepts subject_kind={selector_kind!r} "
                f"(got {subject_kind!r})"
            )
        item = QueueItem(
            queue_id=queue.queue_id,
            subject=subject,
            subject_kind=subject_kind,
            capsule_ref=capsule_ref,
        )
        conn.execute(
            "INSERT INTO annotation_items (item_id, queue_id, state, item_json) "
            "VALUES (?, ?, ?, ?)",
            (
                item.item_id,
                item.queue_id,
                item.state.value,
                item.model_dump_json(exclude_none=True),
            ),
        )
        conn.commit()
        return item
    finally:
        conn.close()


def _selector_sample_hash(subject: str) -> float:
    """Stable [0,1) position for *subject* used by ``SubjectSelector.sample``.

    Deliberately hash-derived rather than ``random``: a queue populated twice
    from the same store must contain the same items. A random sample would
    make the review set unreproducible, which for an evidence product is a
    defect — an auditor asking "why was this run reviewed and that one not?"
    must get a stable answer, not "chance".
    """
    import hashlib

    digest = hashlib.sha256(subject.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def _capsule_matches_selector(
    selector: SubjectSelector,
    *,
    run_id: str,
    tags: list[str],
    tool_names: list[str],
) -> bool:
    """All present selector keys are ANDed (spec §Subject selector)."""
    if selector.run_ids and run_id not in selector.run_ids:
        return False
    if selector.tags and not set(selector.tags) & set(tags):
        return False
    if selector.tool_names and not set(selector.tool_names) & set(tool_names):
        return False
    return True


def _capsule_facts(capsule_dir: Path) -> tuple[str, list[str], list[str]]:
    """Read the selector-relevant facts from a capsule directory."""
    import json as _json

    import yaml as _yaml

    run_id = capsule_dir.name
    tags: list[str] = []
    try:
        manifest = _yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
    except (OSError, _yaml.YAMLError):
        manifest = None
    if isinstance(manifest, dict):
        run_id = str(manifest.get("run_id") or capsule_dir.name)
        meta = manifest.get("metadata")
        raw_tags = (meta or {}).get("tags") if isinstance(meta, dict) else None
        if isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags]
        elif isinstance(raw_tags, str):
            tags = [raw_tags]

    tool_names: list[str] = []
    tool_path = capsule_dir / "tool-calls.jsonl"
    if tool_path.is_file():
        for line in tool_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = _json.loads(line)
            except ValueError:
                continue  # a malformed line must not abort population
            if isinstance(record, dict):
                name = record.get("tool_name") or record.get("name")
                if name:
                    tool_names.append(str(name))
    return run_id, tags, tool_names


def populate_queue(
    queue_ref: str,
    capsule_root: Path,
    *,
    db_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Enqueue every stored capsule matching the queue's selector (ADR-0118 P2).

    Idempotent: a subject already on the queue is skipped, so re-running after
    new capsules land adds only the new ones. That makes this safe to put on a
    schedule, which is the point — a review queue nobody refills is a review
    queue nobody uses.

    ``sample`` is applied **deterministically** (see
    :func:`_selector_sample_hash`), so repeat runs select the same subjects.

    Returns a summary; with *dry_run* nothing is written.
    """
    queue = get_queue(queue_ref, db_path=db_path)
    selector = queue.subject_selector

    if selector.subject_kind == "span":
        raise AnnotationError(
            "cannot auto-populate a span-scoped queue: spans are not enumerable "
            "from the capsule store without a span selector. Enqueue spans "
            "explicitly with `nova annotate queue add --subject sha256:...`."
        )

    existing = {item.subject for item in list_items(queue_ref, db_path=db_path)}
    scanned = matched = added = 0
    skipped_existing = skipped_sample = 0

    for child in sorted(Path(capsule_root).iterdir()) if Path(capsule_root).is_dir() else []:
        if not (child.is_dir() and (child / "capsule.yaml").is_file()):
            continue
        scanned += 1
        run_id, tags, tool_names = _capsule_facts(child)
        if not _capsule_matches_selector(
            selector, run_id=run_id, tags=tags, tool_names=tool_names
        ):
            continue
        matched += 1

        subject = annotation_subject_digest(child)
        if selector.sample is not None and _selector_sample_hash(subject) >= selector.sample:
            skipped_sample += 1
            continue
        if subject in existing:
            skipped_existing += 1
            continue
        if not dry_run:
            enqueue_item(
                queue_ref,
                subject=subject,
                subject_kind="capsule",
                capsule_ref=str(child),
                db_path=db_path,
            )
        existing.add(subject)
        added += 1

    return {
        "queue": queue.name,
        "scanned": scanned,
        "matched": matched,
        "added": added,
        "skipped_existing": skipped_existing,
        "skipped_sample": skipped_sample,
        "dry_run": dry_run,
    }


def list_items(
    queue_ref: str | None = None,
    state: ItemState | None = None,
    db_path: Path | None = None,
) -> list[QueueItem]:
    """List items (oldest first — ULIDs are time-ordered), optionally filtered."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        clauses: list[str] = []
        params: list[str] = []
        if queue_ref is not None:
            queue = _get_queue(conn, queue_ref)
            clauses.append("queue_id = ?")
            params.append(queue.queue_id)
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT item_json FROM annotation_items {where} ORDER BY item_id",  # noqa: S608
            params,
        ).fetchall()
        return [_row_to_item(r) for r in rows]
    finally:
        conn.close()


def get_item(item_id: str, db_path: Path | None = None) -> QueueItem:
    """Load one item by its ULID."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        return _get_item(conn, item_id)
    finally:
        conn.close()


def claim_next(
    reviewer: str,
    queue_ref: str | None = None,
    db_path: Path | None = None,
) -> QueueItem | None:
    """Claim the next ``pending`` item for *reviewer* (``pending → assigned``).

    Round-robin order is oldest-first by ULID. Returns ``None`` when nothing is
    pending (an empty queue is not an error). The transition is a single
    state-guarded ``UPDATE`` — if two reviewers race, exactly one wins each item.
    """
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        queue_id: str | None = None
        if queue_ref is not None:
            queue_id = _get_queue(conn, queue_ref).queue_id
        while True:
            where = "state = ?"
            params: list[str] = [ItemState.PENDING.value]
            if queue_id is not None:
                where += " AND queue_id = ?"
                params.append(queue_id)
            row = conn.execute(
                f"SELECT item_json FROM annotation_items WHERE {where} "  # noqa: S608
                "ORDER BY item_id LIMIT 1",
                params,
            ).fetchone()
            if row is None:
                return None
            candidate = _row_to_item(row)
            claimed = candidate.model_copy(
                update={
                    "state": ItemState.ASSIGNED,
                    "assignee": reviewer,
                    "assigned_at": _now_iso(),
                }
            )
            if _update_item(conn, claimed, expect_state=ItemState.PENDING):
                return claimed
            # Lost the race for this item — try the next pending one.
    finally:
        conn.close()


def claim_item(
    item_id: str, reviewer: str, db_path: Path | None = None
) -> QueueItem:
    """Claim one named ``pending`` item (the ``manual`` assignment policy, D1)."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        item = _get_item(conn, item_id)
        if item.state is not ItemState.PENDING:
            raise ItemStateError(
                f"item {item_id} is {item.state.value!r}, not 'pending' — cannot claim"
            )
        claimed = item.model_copy(
            update={
                "state": ItemState.ASSIGNED,
                "assignee": reviewer,
                "assigned_at": _now_iso(),
            }
        )
        if not _update_item(conn, claimed, expect_state=ItemState.PENDING):
            raise ItemStateError(f"item {item_id} was claimed concurrently — retry")
        return claimed
    finally:
        conn.close()


# ── Submission (assigned → completed | checker_pending) ───────────────────────


def _coerce_value(config: ScoreConfig, raw: object) -> bool | float | str:
    """Coerce a CLI/raw value to the config's ``value_type`` — reject, never guess."""
    if config.value_type is ScoreValueType.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in ("true", "yes", "1", "pass"):
                return True
            if low in ("false", "no", "0", "fail"):
                return False
        raise CriteriaError(
            f"criterion {config.name!r} expects a boolean (true/false), got {raw!r}"
        )
    if config.value_type is ScoreValueType.NUMERIC:
        if isinstance(raw, bool):
            raise CriteriaError(f"criterion {config.name!r} expects a number, got {raw!r}")
        if isinstance(raw, (int, float)):
            return float(raw)
        try:
            return float(str(raw))
        except ValueError:
            raise CriteriaError(
                f"criterion {config.name!r} expects a number, got {raw!r}"
            ) from None
    return str(raw)


def _resolve_criteria(
    queue: AnnotationQueue,
    values: Mapping[str, object],
    skip_criteria: Iterable[str],
    db_path: Path | None,
) -> list[tuple[ScoreConfig, bool | float | str]]:
    """Resolve every graded criterion to (config, coerced value) — all-or-nothing."""
    skipped = set(skip_criteria)
    unknown_skips = skipped - set(queue.criteria)
    if unknown_skips:
        raise CriteriaError(
            f"cannot skip criteria not defined by the queue: {sorted(unknown_skips)}"
        )
    expected = [c for c in queue.criteria if c not in skipped]
    if not expected:
        raise CriteriaError("all criteria skipped — nothing to grade; use skip instead")
    supplied = set(values)
    missing = [c for c in expected if c not in supplied]
    if missing:
        raise CriteriaError(
            f"missing criteria {missing} — grade them or skip them explicitly "
            f"(queue defines {queue.criteria})"
        )
    extra = sorted(supplied - set(expected))
    if extra:
        raise CriteriaError(f"criteria not defined by the queue (or skipped): {extra}")
    resolved: list[tuple[ScoreConfig, bool | float | str]] = []
    for criterion in expected:
        config = find_config_for_score(criterion, db_path=db_path)
        if config is None:
            raise CriteriaError(
                f"criterion {criterion!r} has no registered score config (ADR-0117)"
            )
        resolved.append((config, _coerce_value(config, values[criterion])))
    return resolved


def submit_item(
    item_id: str,
    values: Mapping[str, object],
    reviewer: str | None = None,
    note: str | None = None,
    skip_criteria: Iterable[str] = (),
    db_path: Path | None = None,
) -> tuple[QueueItem, list[Score]]:
    """Grade an ``assigned`` item: validate, append ``HUMAN`` scores, transition.

    Validation is all-or-nothing: every non-skipped criterion must be supplied,
    resolve to an ADR-0117 config, coerce to its ``value_type``, and pass
    :func:`validate_score_against_config` **before** any append. The scores land
    in ``<capsule_ref>/scores.jsonl`` via the existing ``append_score`` path with
    ``source=human``, ``evaluator_id=assignee`` and ``eval_card_digest`` set to
    the config's ``content_digest``. The submission is Ed25519-signed with the
    maker's keyring key; the item then reaches ``completed``, or
    ``checker_pending`` when the queue requires a checker (D4).
    """
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        item = _get_item(conn, item_id)
        if item.state is not ItemState.ASSIGNED:
            raise ItemStateError(
                f"item {item_id} is {item.state.value!r}, not 'assigned' — claim it first"
            )
        assignee = item.assignee or ""
        if reviewer is not None and reviewer != assignee:
            raise SeparationOfDutiesError(
                f"item {item_id} is assigned to {assignee!r}, not {reviewer!r} — "
                "only the assignee may submit"
            )
        queue = _get_queue(conn, item.queue_id)
        resolved = _resolve_criteria(queue, values, skip_criteria, db_path)

        if item.capsule_ref is None:
            raise ItemStateError(
                f"item {item_id} has no capsule_ref — nowhere to append scores"
            )
        capsule_dir = Path(item.capsule_ref)
        if not capsule_dir.is_dir():
            raise ItemStateError(
                f"capsule directory not found: {capsule_dir} — the item stays "
                "assigned and the submit is retryable"
            )

        submitted_at = _now_iso()
        scores: list[Score] = []
        for config, value in resolved:
            score = Score(
                subject=item.subject,
                subject_kind=item.subject_kind,
                name=config.name,
                value=value,
                value_type=config.value_type,
                source=ScoreSource.HUMAN,
                evaluator_id=assignee,
                eval_card_digest=config.content_digest,
                created_at=submitted_at,
            )
            validate_score_against_config(score, config)  # no partial write
            scores.append(score)

        scores_path = capsule_dir / SCORES_FILENAME
        for score in scores:
            append_score(scores_path, score)

        private_key, key_fp = ensure_keypair(assignee)
        score_ids = [s.score_id for s in scores]
        signature = sign_payload(
            private_key,
            submission_payload(item.item_id, item.subject, score_ids, submitted_at),
        )
        extensions = dict(item.extensions or {})
        extensions.update(
            {
                _EXT_MAKER_FP: key_fp,
                _EXT_MAKER_SIG: signature,
                _EXT_SUBMITTED_AT: submitted_at,
            }
        )
        next_state = (
            ItemState.CHECKER_PENDING if queue.require_checker else ItemState.COMPLETED
        )
        updated = item.model_copy(
            update={
                "state": next_state,
                "resulting_score_ids": score_ids,
                "completed_at": None if queue.require_checker else submitted_at,
                "note": note if note is not None else item.note,
                "extensions": extensions,
            }
        )
        if not _update_item(conn, updated, expect_state=ItemState.ASSIGNED):
            raise ItemStateError(
                f"item {item_id} changed state concurrently during submit"
            )
        return updated, scores
    finally:
        conn.close()


def confirm_item(
    item_id: str,
    checker: str,
    note: str | None = None,
    db_path: Path | None = None,
) -> QueueItem:
    """Checker step (D4): finalize a ``checker_pending`` item.

    Separation of duties is enforced at the crypto level, mirroring
    :mod:`novafabric.registry.protected_labels`: the checker's identity **and**
    Ed25519 keyring fingerprint must both differ from the maker's.
    """
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        item = _get_item(conn, item_id)
        if item.state is not ItemState.CHECKER_PENDING:
            raise ItemStateError(
                f"item {item_id} is {item.state.value!r}, not 'checker_pending' — "
                "nothing to confirm"
            )
        if checker == item.assignee:
            raise SeparationOfDutiesError(
                "checker equals the maker — a distinct reviewer must confirm "
                "(ADR-0118 D4 / ADR-0003)"
            )
        private_key, key_fp = ensure_keypair(checker)
        maker_fp = (item.extensions or {}).get(_EXT_MAKER_FP)
        if maker_fp is not None and key_fp == maker_fp:
            raise SeparationOfDutiesError(
                "checker key fingerprint matches the maker's — two distinct "
                "identities (keys) are required (ADR-0118 D4 / ADR-0003)"
            )
        confirmed_at = _now_iso()
        signature = sign_payload(
            private_key, confirmation_payload(item.item_id, checker, confirmed_at)
        )
        extensions = dict(item.extensions or {})
        extensions.update(
            {
                _EXT_CHECKER_FP: key_fp,
                _EXT_CHECKER_SIG: signature,
                _EXT_CONFIRMED_AT: confirmed_at,
            }
        )
        updated = item.model_copy(
            update={
                "state": ItemState.COMPLETED,
                "checker": checker,
                "completed_at": confirmed_at,
                "note": note if note is not None else item.note,
                "extensions": extensions,
            }
        )
        if not _update_item(conn, updated, expect_state=ItemState.CHECKER_PENDING):
            raise ItemStateError(
                f"item {item_id} changed state concurrently during confirm"
            )
        return updated
    finally:
        conn.close()


def skip_item(
    item_id: str, note: str | None = None, db_path: Path | None = None
) -> QueueItem:
    """Skip an item (terminal, writes no score) from ``pending`` or ``assigned``."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        item = _get_item(conn, item_id)
        if item.state not in (ItemState.PENDING, ItemState.ASSIGNED):
            raise ItemStateError(
                f"item {item_id} is {item.state.value!r} — only 'pending' or "
                "'assigned' items can be skipped"
            )
        updated = item.model_copy(
            update={
                "state": ItemState.SKIPPED,
                "note": note if note is not None else item.note,
            }
        )
        if not _update_item(conn, updated, expect_state=item.state):
            raise ItemStateError(f"item {item_id} changed state concurrently during skip")
        return updated
    finally:
        conn.close()


# ── Subject digests ────────────────────────────────────────────────────────────


def annotation_subject_digest(capsule_dir: str | Path) -> str:
    """Content-addressed ``sha256:`` digest of a capsule for use as an item subject.

    RFC 6962-style Merkle root over the capsule files (same construction as
    ``evidence.merkle.capsule_merkle_root``) **excluding** the annotation streams
    (``scores.jsonl`` and ``comments.jsonl``): annotating evidence must never
    alter the identity of the evidence being annotated, so successive review
    rounds on the same capsule share one stable subject.
    """
    from novafabric.capsule.comments import COMMENTS_FILENAME
    from novafabric.evidence.merkle import _leaf, _merkle_root

    root = Path(capsule_dir)
    excluded = {SCORES_FILENAME, COMMENTS_FILENAME}
    leaves = [
        _leaf(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in excluded
    ]
    return "sha256:" + _merkle_root(leaves).hex()
