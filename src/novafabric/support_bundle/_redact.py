"""Redaction pipeline for the support bundle (ADR-0187 D2/D3).

Deny-by-default posture: any mapping key matching :data:`DENY_KEY_PATTERN`
has its entire value subtree replaced by :data:`REDACTED` before the bytes
ever reach the staging directory. Environment-variable *values* are never
collected at all (names only — see ``_collect.collect_env_names``).

The ruleset is versioned so the bundle manifest can record exactly which
redaction rules were in force when the bundle was cut (ADR-0187 D4).
"""

from __future__ import annotations

import re
from typing import Any

#: Version of the redaction ruleset recorded in ``manifest.json``.
#: v2 adds line-level redaction (:func:`redact_line`) for log members.
REDACTION_RULESET_VERSION = "v2"

#: Placeholder written in place of any redacted value.
REDACTED = "[REDACTED]"

#: Ruleset v1 — keys matching any of these fragments (case-insensitive)
#: are redacted wholesale. Mirrors the secret classes of ADR-0187 D2 and
#: the env-only secret precedent of ``novafabric.server.config``.
DENY_KEY_PATTERN = re.compile(
    r"(token|secret|password|key|dsn|credential)",
    re.IGNORECASE,
)

#: Ruleset v2 — line-level form of the same deny classes, for free-text log
#: lines: any ``<something-token/secret/…> = value`` or ``…: value`` pair has
#: its value replaced. Applied by :func:`redact_line` to every log line
#: collected into the bundle (ADR-0187 bounded-log slice).
_LINE_SECRET_RE = re.compile(
    r"""(
        [A-Za-z0-9_.\-]*                       # optional key prefix
        (?:token|secret|password|key|dsn|credential)
        [A-Za-z0-9_.\-]*                       # optional key suffix
        ["']?\s*[=:]\s*                        # separator
    )(["']?)([^\s"',;]+)""",
    re.IGNORECASE | re.VERBOSE,
)


def redact_value(value: Any) -> Any:
    """Recursively redact *value* per ruleset v1.

    - dict: any key matching :data:`DENY_KEY_PATTERN` has its whole value
      (including nested structures) replaced by :data:`REDACTED`; other
      values are recursed into.
    - list/tuple: each element is recursed into.
    - scalars: returned unchanged.
    """
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, val in value.items():
            if isinstance(key, str) and DENY_KEY_PATTERN.search(key):
                redacted[key] = REDACTED
            else:
                redacted[key] = redact_value(val)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in value]
    return value


def redact_line(line: str) -> str:
    """Redact secret-bearing ``key=value`` / ``key: value`` pairs in *line*.

    Ruleset v2, for free-text log lines: whenever a key containing one of
    the deny fragments (token/secret/password/key/dsn/credential) is
    followed by ``=`` or ``:`` and a value, the value is replaced by
    :data:`REDACTED`. The key itself is kept so the line stays diagnosable.
    """
    return _LINE_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", line)
