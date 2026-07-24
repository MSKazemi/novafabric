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
#: v3 adds credential *value* patterns (:data:`_VALUE_SECRET_PATTERNS`), so a
#: bare token in free text is caught even when no key names it.
REDACTION_RULESET_VERSION = "v3"

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


#: Ruleset v3 — credential **value** patterns, as ``(rule_id, pattern)``.
#:
#: Why this exists: v1/v2 are key-driven — they only fire when a *key* names
#: a secret (``api_token=…``). A bare credential sitting in free text
#: ("provider rejected sk-ant-…") slipped through, which made a redacted
#: audit line a side channel for exactly what the capsule pipeline strips
#: (ADR-0191 D4 explicitly forbids that). These patterns close it.
#:
#: Scope: the **prefixed** rules of the capsule secret pack
#: (``novafabric.capture.secrets``, gitleaks-core-v0), whose distinctive
#: prefixes make a false positive nearly impossible, plus three ubiquitous
#: prefixed credentials the pack does not carry (GitHub, AWS, Slack).
#:
#: Deliberately **excluded**: the pack's entropy-only rules — a bare 64-hex
#: string, a bare 32- or 40-char alphanumeric, a bare UUID. Those match
#: content this pipeline must *preserve*: ``entry_hash``/``prev_hash`` chain
#: values, content addresses, capsule and run ids. Applying them here would
#: destroy the ADR-0191 D5 integrity guarantee to chase a low-confidence
#: match. A parity test fails CI if a new pack rule is neither covered here
#: nor listed as a deliberate exclusion.
_VALUE_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("novafabric-api-key", r"nvfk_[A-Za-z0-9\-_]{8}_[A-Za-z0-9\-_]{30,60}"),
    # ADR-0205: webhook signing secret — same first-party posture as nvfk_.
    ("novafabric-webhook-secret", r"nvwh_[A-Za-z0-9\-_]{8}_[A-Za-z0-9\-_]{30,60}"),
    ("anthropic-api-key", r"sk-ant-[A-Za-z0-9\-_]{20,80}"),
    ("openai-api-key", r"sk-(?!ant-)[A-Za-z0-9]{20,60}"),
    ("huggingface-token", r"hf_[A-Za-z0-9]{34,50}"),
    ("replicate-api-key", r"r8_[A-Za-z0-9]{37}"),
    ("langfuse-key", r"pk-lf-[A-Za-z0-9\-]{30,50}"),
    ("langsmith-key", r"ls__[A-Za-z0-9]{40,60}"),
    ("weaviate-api-key", r"wcs_[A-Za-z0-9]{30,50}"),
    ("qdrant-api-key", r"qdrant_[A-Za-z0-9]{30,50}"),
    ("github-token", r"gh[pousr]_[A-Za-z0-9]{16,80}"),
    ("aws-access-key-id", r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Za-z0-9])"),
    ("slack-token", r"xox[baprs]-[A-Za-z0-9\-]{10,80}"),
)

_VALUE_SECRET_RE = re.compile(
    "|".join(f"(?:{pattern})" for _rule_id, pattern in _VALUE_SECRET_PATTERNS)
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
    """Redact secrets in a free-text *line*, key-driven then value-driven.

    Ruleset v2 (key-driven): whenever a key containing one of the deny
    fragments (token/secret/password/key/dsn/credential) is followed by
    ``=`` or ``:`` and a value, the value is replaced by :data:`REDACTED`.
    The key itself is kept so the line stays diagnosable.

    Ruleset v3 (value-driven): any remaining substring matching a known
    credential shape (:data:`_VALUE_SECRET_PATTERNS`) is replaced too, so a
    bare token in prose is caught even when nothing names it as a secret.
    Applied second, because the key-driven pass leaves ``[REDACTED]`` behind
    and there is then nothing left for the value pass to match.
    """
    line = _LINE_SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", line)
    return _VALUE_SECRET_RE.sub(REDACTED, line)
