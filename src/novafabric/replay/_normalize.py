"""The declared, versioned volatile-token rule set of the fidelity verdict (ADR-0321 amendment).

A workload prints ids it generates itself: LangChain prefixes message
content-block ids with ``lc_`` and a fresh ``uuid4``, so a replay whose every
model and tool call was served prints different bytes than the run it replays
(AWS evaluation, workload ``log_mcp``: stdout bytes differed only in RFC 4122
UUIDs). The required stdout check therefore compares the stream after this rule
set; the raw byte comparison stays in the result as an advisory check.

Rule set ``volatile-ids/1`` -- one rule, the only one evidence required:

* **RFC 4122 UUIDs** in canonical text form (8-4-4-4-12 hex digits, version
  1-5, variant ``10xx``), case-insensitive, not inside a longer hex run. Each
  distinct UUID is replaced by ``<uuid:N>``, N its order of first appearance,
  so the *structure* survives: an id printed twice in the recording must be
  printed twice in the replay (two different ids there diverge).

Timestamps, durations, hex digests, ``uuid.hex`` (no hyphens) and RFC 9562
versions 6-8 are **not** masked: no evidence needed them. A new rule is a new
version, never an edit of this one, so a result's ``normalization`` field names
exactly the rules that produced its verdict.
"""

from __future__ import annotations

import re
from typing import Final

#: The identifier written to ``fidelity.normalization`` and to each normalised check.
NORMALIZER_ID: Final[str] = "volatile-ids/1"

_UUID: Final[re.Pattern[bytes]] = re.compile(
    rb"(?<![0-9A-Fa-f])"
    rb"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-5][0-9A-Fa-f]{3}-[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}"
    rb"(?![0-9A-Fa-f])"
)


def normalize_volatile(data: bytes) -> bytes:
    """*data* with every RFC 4122 UUID replaced by ``<uuid:N>`` (first-seen order)."""
    seen: dict[bytes, int] = {}

    def _replace(match: re.Match[bytes]) -> bytes:
        key = match.group(0).lower()
        index = seen.setdefault(key, len(seen) + 1)
        return b"<uuid:%d>" % index

    return _UUID.sub(_replace, data)
