"""Append-only audit log for `nova serve` mutations (Layer B).

Per ADR-0027 §1 Layer B: every mutation is recorded in
`~/.novafabric/dashboard-audit.jsonl`, one record per line, append-only.
The user can `cat` it, grep it, or pipe it into their own tooling. There
is no rotation / no truncation; entries are tiny (few hundred bytes each).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from novafabric._paths import dashboard_audit_path

AUDIT_ENV: Final[str] = "NOVAFABRIC_DASHBOARD_AUDIT_FILE"

#: Block size for reverse (tail-first) reads. 64 KiB covers hundreds of
#: typical few-hundred-byte audit records per seek.
TAIL_BLOCK_SIZE: Final[int] = 64 * 1024

logger = logging.getLogger(__name__)

_lock = threading.Lock()


def _path() -> Path:
    return dashboard_audit_path()


#: How well the actor behind a record is actually known (ADR-0231 D3).
#: ``shared-token`` is the honest answer for the one credential every operator
#: pastes into a browser; ``credential`` is an ADR-0228 issued token, which has
#: its own fingerprint and scope; ``federated`` is reserved for an external IdP.
IDENTITY_SOURCES: Final[frozenset[str]] = frozenset(
    {"shared-token", "credential", "federated"}
)

#: Keys the writer can emit. Every one of these must be present in
#: ``audit.siem_export.DASHBOARD_FIELD_ALLOWLIST`` or on its deliberate-exclusion
#: list, or the field is silently dropped from every SIEM export while the local
#: file looks enriched — the exact divergence ADR-0231 D5 exists to prevent.
#: ``tests/serve/test_audit_enrichment.py`` asserts the two agree.
EMITTABLE_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "audit_id",
        "ts",
        "action",
        "args",
        "cli_equivalent",
        "actor_token_fp",
        "actor",
        "result",
        "error",
        "extra",
        "resource",
        "prior",
        "current",
        "redaction",
        "required_scope",
        "held_scope",
    }
)


def _redaction_ruleset() -> str:
    """The ADR-0187 ruleset version, read from the ruleset — never hardcoded.

    A version string copied into a second file is a fact with no owner: it stays
    right until the ruleset is revised, and then silently attests to a version
    that no longer ran.
    """
    try:
        from novafabric.support_bundle._redact import REDACTION_RULESET_VERSION

        return f"adr0187-{REDACTION_RULESET_VERSION}"
    except Exception:  # noqa: BLE001
        return "adr0187-unknown"


def _redact_state(value: Any) -> tuple[Any, list[str]]:
    """Run the ADR-0187 ruleset over captured state. Returns (redacted, findings).

    This is the sharp edge of ADR-0231 D2: adding before/after state capture to an
    append-only, deliberately unrotated file is a direct route to writing secrets
    to disk permanently. CLAUDE.md forbids logging *"secrets, tokens, prompts, or
    env vars outside the redacted capsule"*, so reusing the shipped ruleset —
    rather than hand-rolling a second one that drifts from it — is not optional.

    A redaction failure returns ``None`` and a finding, never the raw value. The
    one thing this must never do is fall back to writing what it could not check.
    """
    if value is None:
        return None, []
    try:
        from novafabric.support_bundle._redact import redact_value

        redacted = redact_value(value)
    except Exception as exc:  # noqa: BLE001 — never write unchecked state
        logger.exception("dashboard audit: state redaction failed; dropping the value")
        return None, [f"redaction_failed:{type(exc).__name__}"]
    findings: list[str] = []
    if redacted != value:
        findings.append("redacted")
    return redacted, findings


def append(
    *,
    action: str,
    args: dict[str, Any],
    cli_equivalent: str,
    actor_token_fp: str,
    result: str = "ok",
    error: str | None = None,
    extra: dict[str, Any] | None = None,
    identity_source: str = "shared-token",
    actor_id: str | None = None,
    resource: str | None = None,
    prior: Any = None,
    current: Any = None,
    required_scope: str | None = None,
    held_scope: str | None = None,
) -> dict[str, Any]:
    """Append one audit record. Returns the record (caller may include it in an API response).

    ``actor_token_fp`` is a short fingerprint of the session token, never the token.

    **ADR-0231 D3 — the record states how well it knows the actor rather than
    implying it knows.** ``actor.identity_source`` is ``shared-token`` unless the
    caller can do better, and ``actor.id`` is *omitted entirely* for a shared
    token: a fingerprint sitting in a field called ``id`` is dishonestly strong
    evidence, and this is the artifact an auditor relies on.

    **D2 — ``prior``/``current`` are captured at the mutation site**, by the
    caller, never by re-reading afterwards: a re-read races concurrent writers and
    records a transition that never occurred. Both pass the ADR-0187 ruleset and
    the record carries ``redaction`` as proof it ran.
    """
    if identity_source not in IDENTITY_SOURCES:
        logger.warning(
            "dashboard audit: unknown identity_source %r; recording shared-token",
            identity_source,
        )
        identity_source = "shared-token"

    actor: dict[str, Any] = {"identity_source": identity_source}
    # Deliberately absent, not null: a shared token has no actor id, and an
    # explicit null still invites a SIEM to render an empty user column as if
    # the field meant something.
    if actor_id and identity_source != "shared-token":
        actor["id"] = actor_id

    record: dict[str, Any] = {
        "audit_id": str(uuid.uuid4()),
        "ts": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "args": args,
        "cli_equivalent": cli_equivalent,
        "actor_token_fp": actor_token_fp,
        "actor": actor,
        "result": result,
    }
    if error:
        record["error"] = error
    if extra:
        record["extra"] = extra
    if resource is not None:
        record["resource"] = resource
    if required_scope is not None:
        record["required_scope"] = required_scope
    if held_scope is not None:
        record["held_scope"] = held_scope
    if prior is not None or current is not None:
        prior_redacted, prior_findings = _redact_state(prior)
        current_redacted, current_findings = _redact_state(current)
        record["prior"] = prior_redacted
        record["current"] = current_redacted
        record["redaction"] = {
            "applied": True,
            "ruleset": _redaction_ruleset(),
            "findings": sorted(set(prior_findings + current_findings)),
        }

    p = _path()
    with _lock:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
        # Ensure read/write only by owner
        try:
            os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    return record


def tail_lines(
    path: Path,
    *,
    before_offset: int | None = None,
    block_size: int = TAIL_BLOCK_SIZE,
) -> Iterator[tuple[int, bytes]]:
    """Yield ``(byte_offset, line)`` newest-first from an append-only JSONL file.

    Reads backwards from EOF (or from ``before_offset``) in ``block_size``
    chunks, so IO is proportional to how far the caller iterates — never the
    whole file. Because the file is append-only, a line's byte offset is a
    stable resume cursor: pass it back as ``before_offset`` to continue with
    strictly older lines. Empty lines are skipped; a missing file yields
    nothing.
    """
    if not path.exists():
        return
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        pos = size if before_offset is None else max(0, min(before_offset, size))
        buf = b""
        while pos > 0:
            read_size = min(block_size, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size) + buf
            parts = buf.split(b"\n")
            if pos > 0:
                # First part may be the tail of a line straddling the block
                # boundary — keep it in the buffer for the next iteration.
                buf = parts[0]
                complete = parts[1:]
                base = pos + len(parts[0]) + 1
            else:
                buf = b""
                complete = parts
                base = 0
            offsets: list[int] = []
            off = base
            for line in complete:
                offsets.append(off)
                off += len(line) + 1
            for line_off, line in zip(reversed(offsets), reversed(complete)):
                if line.strip():
                    yield line_off, line


def read_recent_tail(
    limit: int = 200,
    before_offset: int | None = None,
    action: str | None = None,
) -> tuple[list[dict[str, Any]], int | None]:
    """Return up to *limit* recent audit entries, newest first, with a cursor.

    ``before_offset`` (a byte offset from a previous call's cursor) restricts
    the read to entries strictly before that file position; ``action`` keeps
    only entries whose ``action`` field matches exactly. Malformed lines are
    skipped. Returns ``(entries, next_before_offset)`` where the cursor is
    ``None`` once the file is exhausted.
    """
    p = _path()
    entries: list[dict[str, Any]] = []
    next_cursor: int | None = None
    with _lock:
        for off, raw in tail_lines(p, before_offset=before_offset):
            try:
                rec = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(rec, dict):
                continue
            if action is not None and rec.get("action") != action:
                continue
            entries.append(rec)
            if len(entries) >= limit:
                if off > 0:
                    next_cursor = off
                break
    return entries, next_cursor


def read_recent(limit: int = 200) -> list[dict[str, Any]]:
    """Return the most recent audit entries, newest first."""
    return read_recent_tail(limit)[0]
