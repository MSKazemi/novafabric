"""Session-membership resolution for run capsules (ADR-0122).

Records that a run participates in a multi-turn *session* — an ordered
grouping of N otherwise-independent runs (a conversation or workflow) — as
two additive optional back-reference fields on the capsule manifest:
``session_id`` (ULID) and ``sequence`` (zero-based turn index).

This is **not** the parent/child hierarchy (ADR-0032/0039): that groups the
WORKER capsules of *one* distributed job; a session groups *separate* runs
performed in sequence. The authoritative ordered index is the session
manifest (``session.json``, :mod:`novafabric.session`); the capsule-side
fields resolved here are a convenience back-reference and never required.

NovaFabric **records** these values verbatim from an explicit source; it
never **infers** them. Absent inputs ⇒ absent fields — a capsule without
them is a standalone run, byte-identical to pre-ADR-0122 capsules.

Sources are resolved **atomically per tier** (CLI flags >
``NOVAFABRIC_SESSION_ID``/``NOVAFABRIC_SESSION_SEQUENCE`` env vars > SDK
arguments): the winning tier supplies both fields, so a session id from one
source is never paired with a turn index from another.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: Env vars consulted when no CLI flag supplies the membership (ADR-0122).
SESSION_ID_ENV_VAR = "NOVAFABRIC_SESSION_ID"
SESSION_SEQUENCE_ENV_VAR = "NOVAFABRIC_SESSION_SEQUENCE"

#: Canonical 26-char Crockford base32 ULID (same rule as the capsule schema).
ULID_PATTERN = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")

SessionMembershipSource = Literal["cli-flag", "env-var", "sdk-arg"]


class InvalidSessionMembershipError(ValueError):
    """An explicitly supplied session membership is invalid or incomplete.

    Raised for explicit program input (CLI flags, SDK arguments) so the
    operator gets immediate feedback *before* any capture starts. Invalid
    ambient ``NOVAFABRIC_SESSION_*`` env vars never raise — they are warned
    about and dropped, because a bad shell variable must never block the
    user workload (fail-open).
    """


class ResolvedSessionMembership(BaseModel):
    """A resolved ADR-0122 session back-reference with its provenance."""

    session_id: str
    sequence: int | None = None
    source: SessionMembershipSource


def _parse_sequence(
    raw: object, source: SessionMembershipSource
) -> tuple[int | None, bool]:
    """Parse one tier's sequence candidate.

    Returns ``(value, ok)``. ``ok=False`` means the candidate was invalid;
    for the ambient env tier the caller drops just the sequence (fail-open),
    for explicit tiers this function raises instead.
    """
    if raw is None:
        return None, True
    if isinstance(raw, bool):  # bool is an int subclass — reject explicitly
        candidate: object = raw
    elif isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None, True  # empty normalizes to absent, per spec
        try:
            candidate = int(stripped)
        except ValueError:
            candidate = raw
    else:
        candidate = raw
    if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
        return candidate, True
    message = (
        f"invalid session sequence {raw!r} (from {source}): "
        "must be an integer >= 0 (zero-based turn index)"
    )
    if source == "env-var":
        logger.warning(
            "%s — dropping the sequence, keeping the session id (%s)",
            message,
            SESSION_SEQUENCE_ENV_VAR,
        )
        return None, False
    raise InvalidSessionMembershipError(message)


def _resolve_tier(
    session_id: str | None,
    sequence: object,
    source: SessionMembershipSource,
) -> ResolvedSessionMembership | None:
    """Resolve one tier atomically; behavior depends on how explicit it was."""
    sid = session_id if isinstance(session_id, str) and session_id.strip() else None
    seq_supplied = sequence is not None and (
        not isinstance(sequence, str) or bool(sequence.strip())
    )
    if sid is None and not seq_supplied:
        return None  # tier supplied nothing — fall through

    if sid is None:
        # A turn index without a session is meaningless (ADR-0122 D2).
        message = (
            f"session sequence supplied without a session id (from {source}): "
            "sequence is the turn index *within* a session and requires "
            "session_id"
        )
        if source == "env-var":
            logger.warning("Ignoring session membership from env vars: %s", message)
            return None
        raise InvalidSessionMembershipError(message)

    if not ULID_PATTERN.match(sid):
        message = (
            f"invalid session id {sid!r} (from {source}): must be a 26-character "
            "Crockford base32 ULID (create one with `nova session new`)"
        )
        if source == "env-var":
            logger.warning("Ignoring session membership from env vars: %s", message)
            return None
        raise InvalidSessionMembershipError(message)

    seq, _ok = _parse_sequence(sequence, source)
    return ResolvedSessionMembership(session_id=sid, sequence=seq, source=source)


def resolve_session_membership(
    cli_session_id: str | None = None,
    cli_sequence: int | None = None,
    sdk_session_id: str | None = None,
    sdk_sequence: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> ResolvedSessionMembership | None:
    """Resolve the session back-reference from explicit sources only.

    Precedence mirrors ADR-0126: CLI flags > ``NOVAFABRIC_SESSION_ID`` /
    ``NOVAFABRIC_SESSION_SEQUENCE`` env vars > SDK arguments — resolved
    **atomically per tier** (the first tier that supplies anything wins
    wholly). Empty / whitespace-only values count as *not supplied*. If no
    tier supplies a membership the result is ``None`` and the capsule
    fields stay absent (a standalone run — today's behavior).

    Raises:
        InvalidSessionMembershipError: A CLI/SDK-supplied membership has a
            non-ULID session id, a negative or non-integer sequence, or a
            sequence without a session id. Ambient env-var problems are
            warned about and skipped instead — a bad shell variable never
            blocks the workload.
    """
    env = os.environ if environ is None else environ
    tiers: list[tuple[str | None, object, SessionMembershipSource]] = [
        (cli_session_id, cli_sequence, "cli-flag"),
        (env.get(SESSION_ID_ENV_VAR), env.get(SESSION_SEQUENCE_ENV_VAR), "env-var"),
        (sdk_session_id, sdk_sequence, "sdk-arg"),
    ]
    for session_id, sequence, source in tiers:
        resolved = _resolve_tier(session_id, sequence, source)
        if resolved is not None:
            return resolved
    return None
