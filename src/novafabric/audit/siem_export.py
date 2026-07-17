"""SIEM egress for local audit logs — ADR-0191 first slice (experimental).

One-shot export of a chosen audit source over a time window, in ``jsonl``
(native, zero-mapping-loss) or ``ocsf`` (Open Cybersecurity Schema
Framework) format. NovaFabric's job ends at producing correctly formatted,
correctly redacted lines — the site's own shipper does transport (ADR-0191).

Sources
-------
- ``audit`` — the hash-chained ``novafabric.audit.AuditLog`` JSONL. The
  chain is re-verified during the walk (recomputed ``entry_hash`` vs the
  next entry's ``prev_hash``, mirroring ``AuditLog.verify``); any error is
  surfaced in :class:`ExportResult.chain_errors` so the CLI can exit 3
  while still writing what it exported (ADR-0191 D1/D5).
- ``dashboard`` — the append-only ``nova serve`` mutation log
  (ADR-0027 Layer B, not hash-chained).

Redaction (ADR-0191 D4 — deny-by-default)
-----------------------------------------
Top-level fields pass a strict per-source allowlist
(:data:`AUDIT_FIELD_ALLOWLIST` / :data:`DASHBOARD_FIELD_ALLOWLIST`): a field
not on the list is omitted entirely. Free-form payload fields (``details``,
``args``, ``extra``, ``error``) additionally pass the ADR-0187 support-bundle
ruleset (``novafabric.support_bundle._redact``): deny-pattern keys are
replaced wholesale (ruleset v1) and string values get line-level secret
scrubbing (ruleset v2). Honest note: the ADR-0187 ruleset is reused as-is
for those nested writer-defined payloads because a strict allowlist over
arbitrary ``details`` keys is impractical; the strict allowlist discipline
is applied at the record level, and both halves are versioned in the export
header (``redaction_ruleset`` + ``field_allowlist``).

OCSF mapping (ADR-0191 D2 — conservative, documented)
-----------------------------------------------------
:data:`OCSF_CLASS_MAP` pins every ``AuditEventType`` value to an OCSF class:

- **API Activity (6003)** — policy decisions, holds, deletions, exports,
  retention actions: things done *through* the product's API/CLI surface.
- **Application Lifecycle (6002)** — asset lifecycle transitions
  (promote/approve/rollback/unregister and the maker-checker pair).
- **Authentication (3002)** — reserved; no chained event type maps here
  today. Dashboard ``session.*`` / ``auth*`` actions map here.

Unknown event types fall back to API Activity (6003, activity Unknown) —
never dropped. Anything OCSF has no field for rides verbatim in the OCSF
``unmapped`` object (incl. ``entry_hash``/``prev_hash`` — D5): no silent
information loss. The companion spec ``design/spec/audit-siem-egress-v0.md``
carries the same tables; tests/audit/test_siem_export.py fails CI when a new
event type lacks a mapping.

Import discipline: this module's export path is stdlib-only
(``json``/``hashlib``/``datetime``) plus two pure-stdlib in-repo helpers
(``novafabric.audit._paths``, ``novafabric.support_bundle._redact``) —
no pydantic, no network, ever (ADR-0191 D6; proven by test).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, TextIO

from novafabric.support_bundle._redact import (
    REDACTION_RULESET_VERSION,
    redact_line,
    redact_value,
)

#: Version of the strict per-source field allowlist below (recorded in the
#: export header alongside the reused ADR-0187 ruleset version).
SIEM_ALLOWLIST_VERSION: Final[str] = "v1"

#: OCSF schema version the rendering targets.
OCSF_VERSION: Final[str] = "1.1.0"

# --- OCSF class constants -------------------------------------------------

_API_ACTIVITY: Final[int] = 6003
_APP_LIFECYCLE: Final[int] = 6002
_AUTHENTICATION: Final[int] = 3002

_CLASS_NAMES: Final[dict[int, str]] = {
    _API_ACTIVITY: "API Activity",
    _APP_LIFECYCLE: "Application Lifecycle",
    _AUTHENTICATION: "Authentication",
}

_CATEGORIES: Final[dict[int, tuple[int, str]]] = {
    _API_ACTIVITY: (6, "Application Activity"),
    _APP_LIFECYCLE: (6, "Application Activity"),
    _AUTHENTICATION: (3, "Identity & Access Management"),
}

# Activity ids: API Activity (6003) — 1 Create, 2 Read, 4 Delete, 99 Other;
# Application Lifecycle (6002) — 2 Remove, 99 Other; 0 Unknown everywhere.
_ACTIVITY_OTHER: Final[int] = 99
_ACTIVITY_UNKNOWN: Final[int] = 0

#: ADR-0191 D2 mapping table: audit ``event_type`` value →
#: ``(class_uid, class_name, activity_id)``. Keep in lockstep with
#: ``design/spec/audit-siem-egress-v0.md`` §2 — the golden-corpus test
#: fails when an ``AuditEventType`` member is missing here.
OCSF_CLASS_MAP: Final[dict[str, tuple[int, str, int]]] = {
    "policy.allow": (_API_ACTIVITY, "API Activity", _ACTIVITY_OTHER),
    "policy.deny": (_API_ACTIVITY, "API Activity", _ACTIVITY_OTHER),
    "promote": (_APP_LIFECYCLE, "Application Lifecycle", _ACTIVITY_OTHER),
    "approve": (_APP_LIFECYCLE, "Application Lifecycle", _ACTIVITY_OTHER),
    "hold.create": (_API_ACTIVITY, "API Activity", 1),  # Create
    "hold.release": (_API_ACTIVITY, "API Activity", _ACTIVITY_OTHER),
    "capsule.delete": (_API_ACTIVITY, "API Activity", 4),  # Delete
    "evidence.export": (_API_ACTIVITY, "API Activity", 2),  # Read
    "rollback": (_APP_LIFECYCLE, "Application Lifecycle", _ACTIVITY_OTHER),
    "unregister": (_APP_LIFECYCLE, "Application Lifecycle", 2),  # Remove
    "promote.propose": (_APP_LIFECYCLE, "Application Lifecycle", _ACTIVITY_OTHER),
    "promote.approve": (_APP_LIFECYCLE, "Application Lifecycle", _ACTIVITY_OTHER),
    "retention.action": (_API_ACTIVITY, "API Activity", _ACTIVITY_OTHER),
    # ADR-0192: one entry per attempted ops-alert delivery (outbound webhook
    # attempt made through the product surface — API Activity, Create).
    "alert.delivery": (_API_ACTIVITY, "API Activity", 1),
    # ADR-0193: API-key lifecycle transitions, done through the product's
    # CLI/API surface (credential *logon* events would be Authentication).
    "api_key.create": (_API_ACTIVITY, "API Activity", 1),  # Create
    "api_key.revoke": (_API_ACTIVITY, "API Activity", 4),  # Delete
    "api_key.rotate": (_API_ACTIVITY, "API Activity", 3),  # Update
}

#: Conservative fallback for event types not (yet) in the map: API Activity,
#: activity Unknown — exported, flagged by the corpus test, never dropped.
_OCSF_FALLBACK: Final[tuple[int, str, int]] = (
    _API_ACTIVITY,
    "API Activity",
    _ACTIVITY_UNKNOWN,
)

# --- Deny-by-default field allowlists (ADR-0191 D4) ------------------------

#: Chained-source record fields allowed to leave (schema of ``AuditEntry``;
#: ``entry_hash``/``prev_hash`` are required by D5).
AUDIT_FIELD_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "entry_id",
        "timestamp",
        "event_type",
        "actor",
        "resource_id",
        "details",
        "prev_hash",
        "entry_hash",
    }
)

#: Dashboard-source record fields allowed to leave (``serve.audit.append``
#: schema; ``actor_token_fp`` is already only a short fingerprint).
DASHBOARD_FIELD_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "audit_id",
        "ts",
        "action",
        "args",
        "cli_equivalent",
        "actor_token_fp",
        "result",
        "error",
        "extra",
    }
)

#: Free-form (writer-defined) fields that get the ADR-0187 ruleset applied
#: on top of the record-level allowlist.
_FREEFORM_FIELDS: Final[frozenset[str]] = frozenset(
    {"details", "args", "extra", "error"}
)

_TIMESTAMP_FIELD: Final[dict[str, str]] = {"audit": "timestamp", "dashboard": "ts"}


class SiemExportError(Exception):
    """Raised for invalid export parameters (unknown source/format)."""


@dataclass(frozen=True)
class ExportResult:
    """Outcome of one export walk."""

    entries_exported: int
    chain_errors: list[str] = field(default_factory=list)


# --- canonical hashing (mirror of audit._log; keep in sync) ----------------
# Re-implemented here rather than imported because ``audit._log`` pulls in
# pydantic via ``_models`` and the export path must stay stdlib-only.


def _canonical_json(d: dict[str, Any]) -> str:
    return json.dumps(d, separators=(",", ":"), sort_keys=True)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- source resolution ------------------------------------------------------


def default_source_path(source: str) -> Path:
    """Default on-disk location for *source* (resolved lazily, test-patchable)."""
    if source == "audit":
        from novafabric.audit import _paths

        return _paths.AUDIT_LOG_PATH
    if source == "dashboard":
        from novafabric._paths import dashboard_audit_path

        return dashboard_audit_path()
    raise SiemExportError(f"unknown audit source: {source!r} (expected audit|dashboard)")


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _in_window(ts: datetime | None, since: datetime | None, until: datetime | None) -> bool:
    """``--since`` is inclusive, ``--until`` exclusive; unparsable → kept."""
    if ts is None:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts >= until:
        return False
    return True


# --- redaction (ADR-0191 D4) -------------------------------------------------


def _scrub_freeform(value: Any) -> Any:
    """ADR-0187 ruleset over a free-form subtree: deny-pattern keys (v1) plus
    line-level secret scrubbing of every string value (v2)."""

    def _scrub_strings(v: Any) -> Any:
        if isinstance(v, str):
            return redact_line(v)
        if isinstance(v, dict):
            return {k: _scrub_strings(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_scrub_strings(x) for x in v]
        return v

    return _scrub_strings(redact_value(value))


def redact_record(record: dict[str, Any], source: str) -> dict[str, Any]:
    """Deny-by-default redaction: strict field allowlist, then the ADR-0187
    ruleset over the free-form payload fields. Fields outside the allowlist
    are omitted entirely — they never leave (ADR-0191 D4)."""
    allowlist = (
        AUDIT_FIELD_ALLOWLIST if source == "audit" else DASHBOARD_FIELD_ALLOWLIST
    )
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key not in allowlist:
            continue
        out[key] = _scrub_freeform(value) if key in _FREEFORM_FIELDS else value
    return out


# --- OCSF rendering (ADR-0191 D2/D5) -----------------------------------------


def to_ocsf(record: dict[str, Any], *, source: str) -> dict[str, Any]:
    """Render one (already-redacted) record as an OCSF event dict.

    Consumed fields: the timestamp, the actor identity, and the event
    type/action (which selects the class). Everything else — including
    ``entry_hash``/``prev_hash`` for the chained source (D5) — rides in
    ``unmapped``, verbatim: no silent loss.
    """
    ts_field = _TIMESTAMP_FIELD[source]
    if source == "audit":
        operation = str(record.get("event_type", ""))
        actor_name = record.get("actor")
        class_uid, class_name, activity_id = OCSF_CLASS_MAP.get(
            operation, _OCSF_FALLBACK
        )
    else:
        operation = str(record.get("action", ""))
        actor_name = record.get("actor_token_fp")
        if operation.startswith(("session.", "auth", "login", "logout")):
            class_uid, class_name, activity_id = (
                _AUTHENTICATION,
                _CLASS_NAMES[_AUTHENTICATION],
                _ACTIVITY_OTHER,
            )
        else:
            class_uid, class_name, activity_id = (
                _API_ACTIVITY,
                _CLASS_NAMES[_API_ACTIVITY],
                _ACTIVITY_OTHER,
            )

    ts = _parse_ts(record.get(ts_field))
    time_ms = int(ts.timestamp() * 1000) if ts is not None else 0
    category_uid, category_name = _CATEGORIES[class_uid]

    consumed = {ts_field, "actor", "actor_token_fp", "event_type", "action"}
    unmapped: dict[str, Any] = {
        k: v for k, v in record.items() if k not in consumed
    }
    # The selecting field itself also rides in unmapped so the SIEM keeps the
    # native taxonomy verbatim.
    unmapped["event_type" if source == "audit" else "action"] = operation

    event: dict[str, Any] = {
        "class_uid": class_uid,
        "class_name": class_name,
        "category_uid": category_uid,
        "category_name": category_name,
        "activity_id": activity_id,
        "type_uid": class_uid * 100 + activity_id,
        "time": time_ms,
        "severity_id": 1,  # Informational
        "metadata": {
            "version": OCSF_VERSION,
            "product": {"name": "NovaFabric", "vendor_name": "NovaFabric"},
        },
        "actor": {"user": {"name": actor_name}},
        "api": {"operation": operation},
        "unmapped": unmapped,
    }
    return event


# --- export walk --------------------------------------------------------------


def _header(source: str, fmt: str) -> dict[str, Any]:
    """Header entry recording the redaction-ruleset versions (ADR-0191 D4)."""
    return {
        "nova_siem_export": {
            "adr": "ADR-0191",
            "source": source,
            "format": fmt,
            "redaction_ruleset": f"adr0187-{REDACTION_RULESET_VERSION}",
            "field_allowlist": f"siem-allowlist-{SIEM_ALLOWLIST_VERSION}",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    }


def export_entries(
    *,
    source: str,
    fmt: str,
    out: TextIO,
    path: Path | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> ExportResult:
    """Export *source* over ``[since, until)`` to *out*, one JSON line each.

    Writes the header entry first, then every in-window record after
    redaction. For the chained source the full file is walked (window or
    not) so chain verification covers the whole log; errors are returned,
    not raised — the caller distinguishes them via the exit code (D1/D5).
    """
    if source not in ("audit", "dashboard"):
        raise SiemExportError(
            f"unknown audit source: {source!r} (expected audit|dashboard)"
        )
    if fmt not in ("jsonl", "ocsf"):
        raise SiemExportError(f"unknown format: {fmt!r} (expected jsonl|ocsf)")

    src_path = path if path is not None else default_source_path(source)
    ts_field = _TIMESTAMP_FIELD[source]

    out.write(json.dumps(_header(source, fmt), separators=(",", ":")) + "\n")

    exported = 0
    chain_errors: list[str] = []
    prev_recomputed: str | None = None

    if not src_path.exists():
        return ExportResult(entries_exported=0, chain_errors=chain_errors)

    with src_path.open("r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            raw_line = raw_line.rstrip("\n")
            if not raw_line:
                continue
            try:
                record: dict[str, Any] = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                if source == "audit":
                    chain_errors.append(f"line {lineno}: invalid JSON — {exc}")
                continue

            if source == "audit":
                # Chain walk (mirrors AuditLog.verify): advance with the
                # RECOMPUTED hash so a tampered stored value cannot pass.
                stored_entry_hash = record.get("entry_hash", "")
                stored_prev_hash = record.get("prev_hash")
                if stored_prev_hash != prev_recomputed:
                    chain_errors.append(
                        f"line {lineno}: prev_hash mismatch "
                        f"(stored={stored_prev_hash!r}, expected={prev_recomputed!r})"
                    )
                d_no_hash = {k: v for k, v in record.items() if k != "entry_hash"}
                recomputed = _sha256(_canonical_json(d_no_hash))
                if stored_entry_hash != recomputed:
                    chain_errors.append(
                        f"line {lineno}: entry_hash mismatch "
                        f"(stored={stored_entry_hash!r}, recomputed={recomputed!r})"
                    )
                prev_recomputed = recomputed

            if not _in_window(_parse_ts(record.get(ts_field)), since, until):
                continue

            redacted = redact_record(record, source)
            rendered = (
                to_ocsf(redacted, source=source) if fmt == "ocsf" else redacted
            )
            out.write(json.dumps(rendered, separators=(",", ":")) + "\n")
            exported += 1

    return ExportResult(entries_exported=exported, chain_errors=chain_errors)
