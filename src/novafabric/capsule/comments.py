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

"""Append-only ``Comment`` record — human annotations as portable evidence (ADR-0121).

A comment is the human-authored counterpart to the machine-derived ``Score``
record (``eval/scores.py``, ADR-0099): where a score binds ``(value, evaluator,
subject, verdict)``, a comment binds ``(body, author, subject, created_at)``.
This module defines the additive, optional ``comments.jsonl`` capsule file and
its record model. See ``the private design/spec/capsule-comments-v0.md`` and
``schemas/comment.schema.json`` for the wire contract.

Design invariants (ADR-0121 D1–D4):

- **Additive-first:** ``comments.jsonl`` is optional; a capsule without it stays
  valid. Reading a missing file returns an empty list.
- **Append-only / immutable:** a comment is never edited or deleted in place.
  An *edit* is a new comment whose ``in_reply_to`` references the prior one; a
  *delete* is a tombstone comment (``tombstone: true``). This module therefore
  deliberately exposes **no overwrite function** — only :func:`append_comment`.
- **Content-addressed subject:** a capsule/span/run/score subject is a
  ``sha256:`` digest; an asset subject is an ``asset://<type>/<name>@<version>``
  reference (asset storage is P3, planned — the record shape is shared).
- **Secret hygiene (D4):** ``body`` is capsule text and MUST pass the ADR-0009
  secret-scan gate (:func:`gate_comment_body`) before it is persisted. The
  default is *refuse*; opt-in redaction masks the finding and sets
  ``redaction_applied``. A body emptied by redaction is refused.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterator

from pydantic import BaseModel, ConfigDict, Field, model_validator

from novafabric.capture._ulid import new_ulid

logger = logging.getLogger(__name__)

COMMENT_SCHEMA_VERSION = "0.1.0"

#: Canonical name of the optional per-capsule comment log (mirrors ``scores.jsonl``).
COMMENTS_FILENAME = "comments.jsonl"

# ── Shared identity patterns (reuse capsule/score conventions) ────────────────
_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ASSET_RE = re.compile(r"^asset://[^/]+/[^@]+@.+$")

#: Safety bound for thread resolution (spec: "a thread resolver MUST bound recursion").
_MAX_THREAD_DEPTH = 10_000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class CommentSecretError(ValueError):
    """The comment body failed the ADR-0009 secret-scan gate (D4).

    Raised when a body trips a secret pattern and redaction was not requested,
    or when redaction empties the body entirely. The message names rule ids
    only — never the matched secret bytes.
    """


class CommentThreadCycleError(ValueError):
    """A ``in_reply_to`` chain contains a cycle (reported, never an infinite loop)."""


class SubjectKind(str, Enum):
    """What the ``subject`` reference addresses (ADR-0121 D2)."""

    CAPSULE = "capsule"
    SPAN = "span"
    RUN = "run"
    SCORE = "score"
    ASSET = "asset"


class Comment(BaseModel):
    """A single append-only human annotation (one ``comments.jsonl`` line).

    See ``schemas/comment.schema.json`` for the wire contract. Validation
    enforces ULID ids and the ``subject``/``subject_kind`` agreement: digest
    kinds carry a ``sha256:`` subject, ``asset`` carries an ``asset://`` ref.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = COMMENT_SCHEMA_VERSION
    comment_id: str = Field(default_factory=new_ulid)
    subject: str
    subject_kind: SubjectKind
    author: str = Field(min_length=1)
    body: str = Field(min_length=1)
    created_at: str = Field(default_factory=_now_iso)
    in_reply_to: str | None = None
    tags: list[str] | None = None
    tombstone: bool = False
    redaction_applied: bool = False

    @model_validator(mode="after")
    def _check(self) -> Comment:
        if self.schema_version != COMMENT_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version must be {COMMENT_SCHEMA_VERSION!r}, got {self.schema_version!r}"
            )
        if not _ULID_RE.match(self.comment_id):
            raise ValueError(f"comment_id is not a valid ULID: {self.comment_id!r}")
        if self.in_reply_to is not None and not _ULID_RE.match(self.in_reply_to):
            raise ValueError(f"in_reply_to is not a valid ULID: {self.in_reply_to!r}")
        if self.subject_kind is SubjectKind.ASSET:
            if not _ASSET_RE.match(self.subject):
                raise ValueError(
                    "subject_kind 'asset' requires an 'asset://<type>/<name>@<version>' "
                    f"subject, got {self.subject!r}"
                )
        elif not _SHA256_RE.match(self.subject):
            raise ValueError(
                f"subject_kind {self.subject_kind.value!r} requires a 'sha256:<hex>' "
                f"subject, got {self.subject!r}"
            )
        return self


# ── JSONL IO (append-only; mirrors eval/scores.py) ─────────────────────────────


def read_comments(path: str | Path) -> list[Comment]:
    """Read and validate every ``Comment`` from a ``comments.jsonl`` file.

    Returns an empty list if the file does not exist (additive-first: a capsule
    without a comment log is valid). Blank lines are skipped.
    """
    return list(iter_comments(path))


def iter_comments(path: str | Path) -> Iterator[Comment]:
    """Stream ``Comment`` records from a ``comments.jsonl`` file (memory-bounded)."""
    p = Path(path)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                yield Comment.model_validate_json(line)
            except Exception as exc:  # noqa: BLE001 - re-raise with file context
                raise ValueError(f"{p}:{line_no}: invalid Comment record: {exc}") from exc


def append_comment(path: str | Path, comment: Comment) -> None:
    """Append one ``Comment`` as a JSONL line, creating the file if needed.

    This is the **only** write operation on a comment log (append-only
    invariant, ADR-0121 D3): there is no overwrite, edit, or delete API.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(comment.model_dump_json(exclude_none=True) + "\n")


# ── Reader-side semantics (threads + tombstones) ───────────────────────────────


def resolve_thread(comments: list[Comment], comment_id: str) -> list[Comment]:
    """Resolve the ``in_reply_to`` chain containing *comment_id*, root first.

    Walks parent links iteratively with a seen-set, so a malformed cycle is
    **reported** (:class:`CommentThreadCycleError`) rather than looping forever
    (spec edge case "Reply cycle"). A missing parent is tolerated: the reply is
    treated as an orphan root and a warning is logged (spec edge case
    "``in_reply_to`` targets a missing comment").
    """
    by_id = {c.comment_id: c for c in comments}
    if comment_id not in by_id:
        raise KeyError(f"comment {comment_id!r} not found")
    chain: list[Comment] = []
    seen: set[str] = set()
    current: Comment | None = by_id[comment_id]
    while current is not None and len(chain) < _MAX_THREAD_DEPTH:
        if current.comment_id in seen:
            raise CommentThreadCycleError(
                f"in_reply_to cycle detected at comment {current.comment_id!r}"
            )
        seen.add(current.comment_id)
        chain.append(current)
        parent_id = current.in_reply_to
        if parent_id is None:
            break
        parent = by_id.get(parent_id)
        if parent is None:
            logger.warning(
                "comment %s replies to missing comment %s; treating as orphan root",
                current.comment_id,
                parent_id,
            )
            break
        current = parent
    return list(reversed(chain))


def apply_tombstones(comments: list[Comment]) -> list[Comment]:
    """Default reader view: hide retracted comments and the tombstone markers.

    A tombstone record retracts the comment named by its ``in_reply_to``. The
    underlying bytes are never removed (append-only); this is a *view* only.
    A tombstone with an unknown or absent target retracts nothing beyond
    itself and logs a warning (spec edge case "tombstone of an unknown id").
    """
    known = {c.comment_id for c in comments}
    retracted: set[str] = set()
    for c in comments:
        if not c.tombstone:
            continue
        if c.in_reply_to is None or c.in_reply_to not in known:
            logger.warning(
                "tombstone %s targets unknown comment %s", c.comment_id, c.in_reply_to
            )
            continue
        retracted.add(c.in_reply_to)
    return [c for c in comments if not c.tombstone and c.comment_id not in retracted]


# ── ADR-0009 secret-scan gate (D4) ────────────────────────────────────────────


def scan_comment_body(body: str) -> list[str]:
    """Rule ids of ADR-0009 secret patterns matching *body* (empty = clean).

    Reuses the exact rules the capsule sealer applies (``capture.secrets``),
    so a comment body is held to the same standard as any other capsule text.
    """
    from novafabric.capture.secrets import _RULES

    return [str(rule["id"]) for rule in _RULES if rule["pattern"].search(body)]


def gate_comment_body(body: str, *, redact: bool = False) -> tuple[str, bool]:
    """Pass *body* through the ADR-0009 gate; return ``(stored_body, redacted)``.

    Default (``redact=False``) **refuses** a body that trips any secret rule —
    a leaked token never enters the evidence store. With ``redact=True`` the
    matches are masked in place (``[REDACTED:<rule_id>]``) and the second
    element of the result is ``True`` so the caller can set
    ``redaction_applied``. A body emptied by redaction is refused (an
    all-secret comment carries no evidence value).
    """
    findings = scan_comment_body(body)
    if not findings:
        return body, False
    if not redact:
        raise CommentSecretError(
            "comment body refused: secret pattern(s) matched: "
            + ", ".join(sorted(set(findings)))
            + " (re-run with redaction enabled to mask, or remove the secret)"
        )
    from novafabric.capture.secrets import _RULES, _replacement

    redacted_body = body
    for rule in _RULES:
        rule_id = str(rule["id"])
        redacted_body = rule["pattern"].sub(
            lambda m, rid=rule_id: _replacement(rid, "mask", m.group()),
            redacted_body,
        )
    if not redacted_body.strip():
        raise CommentSecretError("comment body refused: empty after secret redaction")
    return redacted_body, True


# ── Subject resolution ─────────────────────────────────────────────────────────


def capsule_subject_digest(capsule_dir: str | Path) -> str:
    """Content-addressed ``sha256:`` digest of a capsule for use as a comment subject.

    RFC 6962-style Merkle root over the capsule files (same construction as
    ``evidence.merkle.capsule_merkle_root``) **excluding** ``comments.jsonl``
    itself: the annotation stream must never alter the identity of the
    evidence it annotates, so successive comments on the same capsule share
    one stable subject. On a capsule without a comment log this equals
    ``capsule_merkle_root`` exactly.
    """
    from novafabric.evidence.merkle import _leaf, _merkle_root

    root = Path(capsule_dir)
    leaves = [
        _leaf(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != COMMENTS_FILENAME
    ]
    return "sha256:" + _merkle_root(leaves).hex()
