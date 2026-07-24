"""SIEM egress for local audit logs — ADR-0191 (experimental).

One-shot export of a chosen audit source over a time window, in ``jsonl``
(native, zero-mapping-loss), ``ocsf`` (Open Cybersecurity Schema
Framework) or ``cef`` (ArcSight Common Event Format) format. NovaFabric's
job ends at producing correctly formatted, correctly redacted lines — the
site's own shipper does transport (ADR-0191).

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

CEF mapping (ADR-0191 D2 — legacy collectors)
---------------------------------------------
``cef`` renders each record as one ArcSight CEF:0 line. The class selection
above is reused verbatim, so OCSF and CEF never disagree about what an event
*is*: the CEF ``name`` header carries the OCSF class name, the signature id
carries the **native** ``event_type``/``action`` (the product taxonomy is
never flattened away), and ``cs5Label=ocsfClassUid`` carries the numeric
class. Severity is a constant ``3`` — matching the OCSF ``severity_id: 1``
stance that severity semantics are a SIEM-side decision, not ours.

Unlike the JSON formats, a CEF stream is **pure CEF**: the manifest header
is itself rendered as a CEF event (signature ``nova:siem.export.manifest``)
rather than a JSON line, so a collector can ingest the file without a
special case for line 1. No-silent-loss (D5) is preserved by mapping
``entry_hash``/``prev_hash`` to labelled custom strings and packing every
remaining (already-redacted) field into ``cs6`` as compact JSON.

Import discipline: this module's export path is stdlib-only
(``json``/``hashlib``/``datetime``) plus two pure-stdlib in-repo helpers
(``novafabric.audit._paths``, ``novafabric.support_bundle._redact``) —
no pydantic, no network, ever (ADR-0191 D6; proven by test).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol, TextIO

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
    # ADR-0207: one entry per `nova import` run (batch ingest through the
    # product's CLI surface — API Activity, Create; refusals included).
    "capsule.import": (_API_ACTIVITY, "API Activity", 1),  # Create
    # ADR-0210: one entry per REST erasure request (GDPR Art.17 crypto-shred
    # through the product's API surface — DEK destruction, so Delete; failed
    # and deferred outcomes ride the same event with state in details).
    "erasure.request": (_API_ACTIVITY, "API Activity", 4),  # Delete
    # ADR-0205: webhook subscription registry lifecycle + delivery trail
    # (all through the product's /v0/webhooks API surface — API Activity).
    "webhook.create": (_API_ACTIVITY, "API Activity", 1),  # Create
    "webhook.update": (_API_ACTIVITY, "API Activity", 3),  # Update
    "webhook.delete": (_API_ACTIVITY, "API Activity", 4),  # Delete
    "webhook.ping": (_API_ACTIVITY, "API Activity", _ACTIVITY_OTHER),
    "webhook.redeliver": (_API_ACTIVITY, "API Activity", _ACTIVITY_OTHER),
    # One entry per attempted delivery (outbound POST — mirror alert.delivery).
    "webhook.delivery": (_API_ACTIVITY, "API Activity", 1),
    # Bounded-loss signal: dispatch queue overflowed (drop-with-audit).
    "webhook.queue.overflow": (_API_ACTIVITY, "API Activity", _ACTIVITY_OTHER),
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

#: Per-source strict allowlist (ADR-0191 D4). Registering a source here is
#: the single act that makes it exportable — see :data:`KNOWN_SOURCES`.
_FIELD_ALLOWLISTS: Final[dict[str, frozenset[str]]] = {
    "audit": AUDIT_FIELD_ALLOWLIST,
    "dashboard": DASHBOARD_FIELD_ALLOWLIST,
}

#: The sources this module can export. Derived from the allowlist registry
#: rather than hand-maintained, so a source can never become exportable
#: without someone having reviewed which of its fields may leave.
KNOWN_SOURCES: Final[frozenset[str]] = frozenset(_FIELD_ALLOWLISTS)


class LineSink(Protocol):
    """Anything the export/follow path can write rendered lines to.

    Structural rather than nominal so ``sys.stdout``, an ``io.StringIO`` and
    :class:`novafabric.audit.sinks.RotatingFileSink` all satisfy it without
    this module importing any of them.
    """

    def write(self, text: str, /) -> int: ...

    def flush(self) -> None: ...


class SiemExportError(Exception):
    """Raised for invalid export parameters (unknown source/format)."""


def _check_source(source: str) -> None:
    """Fail loudly on an unregistered source.

    Deliberately strict: the redaction allowlist, the timestamp field and
    the CEF field partition are all per-source, so a source that is merely
    *assumed* to resemble another would silently inherit that one's
    allowlist and leak fields nobody reviewed for its record shape. A new
    source must be registered in :data:`_FIELD_ALLOWLISTS`,
    :data:`_TIMESTAMP_FIELD` and :data:`_CEF_CONSUMED` — the completeness
    test in tests/audit/test_siem_export.py enforces all three.
    """
    if source not in KNOWN_SOURCES:
        raise SiemExportError(
            f"unknown audit source: {source!r} "
            f"(expected {'|'.join(sorted(KNOWN_SOURCES))})"
        )


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
    _check_source(source)
    raise SiemExportError(f"audit source {source!r} has no default path")


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
    _check_source(source)
    allowlist = _FIELD_ALLOWLISTS[source]
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
    _check_source(source)
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


# --- CEF rendering (ADR-0191 D2) ---------------------------------------------

#: CEF format revision emitted in the ``CEF:<n>`` prefix.
CEF_VERSION: Final[int] = 0

CEF_VENDOR: Final[str] = "NovaFabric"
CEF_PRODUCT: Final[str] = "NovaFabric"

#: Constant CEF severity (0–10 scale). Deliberately fixed at 3 ("Low") to
#: mirror the OCSF ``severity_id: 1`` stance: NovaFabric states what
#: happened; how alarming it is, is the SIEM's policy call.
CEF_SEVERITY: Final[int] = 3

#: CEF header fields escape a backslash and the ``|`` separator only.
_CEF_HEADER_ESCAPE: Final[dict[int, str]] = str.maketrans(
    {"\\": "\\\\", "|": "\\|"}
)

#: CEF extension *values* escape a backslash, the ``=`` separator, and any
#: newline (a CEF event is one line by definition).
_CEF_EXT_ESCAPE: Final[dict[int, str]] = str.maketrans(
    {"\\": "\\\\", "=": "\\=", "\n": "\\n", "\r": "\\r"}
)

#: Fields consumed into named CEF extension keys per source; everything left
#: over is packed into ``cs6`` as compact JSON (D5 — no silent loss).
_CEF_CONSUMED: Final[dict[str, frozenset[str]]] = {
    "audit": frozenset(
        {
            "timestamp",
            "actor",
            "event_type",
            "entry_id",
            "resource_id",
            "entry_hash",
            "prev_hash",
        }
    ),
    "dashboard": frozenset(
        {"ts", "actor_token_fp", "action", "audit_id", "result", "cli_equivalent"}
    ),
}


def _cef_header_field(value: Any) -> str:
    return str(value).translate(_CEF_HEADER_ESCAPE)


def _cef_ext_value(value: Any) -> str:
    return str(value).translate(_CEF_EXT_ESCAPE)


def _cef_line(signature_id: str, name: str, extension: list[tuple[str, Any]]) -> str:
    """Assemble one CEF:0 line from an already-ordered extension list.

    Extension pairs whose value is ``None`` or empty are dropped — CEF has no
    representation for "present but null", and emitting ``key=`` confuses
    collectors.
    """
    from novafabric import __version__

    header = "|".join(
        [
            f"CEF:{CEF_VERSION}",
            _cef_header_field(CEF_VENDOR),
            _cef_header_field(CEF_PRODUCT),
            _cef_header_field(__version__),
            _cef_header_field(signature_id),
            _cef_header_field(name),
            str(CEF_SEVERITY),
        ]
    )
    parts = [
        f"{key}={_cef_ext_value(value)}"
        for key, value in extension
        if value is not None and value != ""
    ]
    return f"{header}|{' '.join(parts)}"


def to_cef(record: dict[str, Any], *, source: str) -> str:
    """Render one (already-redacted) record as a single ArcSight CEF:0 line.

    Class selection is shared with :func:`to_ocsf`, so the two formats never
    disagree about what an event is. The signature id keeps the **native**
    ``event_type``/``action`` string; the OCSF numeric class rides in
    ``cs5``; and every field not consumed into a named extension key is
    packed verbatim into ``cs6`` as compact JSON (ADR-0191 D5).
    """
    _check_source(source)
    ts_field = _TIMESTAMP_FIELD[source]
    if source == "audit":
        operation = str(record.get("event_type", ""))
        actor_name = record.get("actor")
        class_uid, class_name, _activity = OCSF_CLASS_MAP.get(
            operation, _OCSF_FALLBACK
        )
    else:
        operation = str(record.get("action", ""))
        actor_name = record.get("actor_token_fp")
        if operation.startswith(("session.", "auth", "login", "logout")):
            class_uid, class_name = _AUTHENTICATION, _CLASS_NAMES[_AUTHENTICATION]
        else:
            class_uid, class_name = _API_ACTIVITY, _CLASS_NAMES[_API_ACTIVITY]

    ts = _parse_ts(record.get(ts_field))
    extension: list[tuple[str, Any]] = [
        ("rt", int(ts.timestamp() * 1000) if ts is not None else None),
        ("suser", actor_name),
    ]

    if source == "audit":
        extension += [
            ("externalId", record.get("entry_id")),
            ("cs1Label", "entryHash"),
            ("cs1", record.get("entry_hash")),
            ("cs2Label", "prevHash"),
            ("cs2", record.get("prev_hash")),
            ("cs3Label", "resourceId"),
            ("cs3", record.get("resource_id")),
        ]
    else:
        extension += [
            ("externalId", record.get("audit_id")),
            ("outcome", record.get("result")),
            ("cs1Label", "cliEquivalent"),
            ("cs1", record.get("cli_equivalent")),
        ]

    remainder = {
        k: v for k, v in record.items() if k not in _CEF_CONSUMED[source]
    }
    extension += [
        ("cs5Label", "ocsfClassUid"),
        ("cs5", class_uid),
        ("cs6Label", "novaUnmapped"),
        ("cs6", _canonical_json(remainder) if remainder else None),
    ]

    return _cef_line(operation, f"{class_name}: {operation}", extension)


# --- shared record pipeline ---------------------------------------------------


def render_record(record: dict[str, Any], *, source: str, fmt: str) -> str:
    """Redact *record* and render it in *fmt* — the one place formats branch.

    Shared by the one-shot export and follow mode so the two can never drift
    in redaction or rendering.
    """
    redacted = redact_record(record, source)
    if fmt == "cef":
        return to_cef(redacted, source=source)
    if fmt == "ocsf":
        return json.dumps(to_ocsf(redacted, source=source), separators=(",", ":"))
    return json.dumps(redacted, separators=(",", ":"))


#: Cap on retained chain-error strings. Follow mode is long-running, so an
#: unbounded list would be a slow memory leak on a persistently broken chain
#: (bounded-everything discipline). The count is still exact.
MAX_RETAINED_CHAIN_ERRORS: Final[int] = 100


class _ChainVerifier:
    """Streaming hash-chain walk, shared by export and follow.

    Mirrors ``AuditLog.verify``: each entry's ``entry_hash`` is recomputed
    and the next entry's ``prev_hash`` is compared against the **recomputed**
    (not stored) value, so a tampered earlier entry cascades. Parses the line
    too, returning ``None`` for blank/undecodable input.
    """

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._prev_recomputed: str | None = None
        self.errors: list[str] = []
        self.error_count = 0

    def _record_error(self, message: str) -> None:
        self.error_count += 1
        if len(self.errors) < MAX_RETAINED_CHAIN_ERRORS:
            self.errors.append(message)
        elif len(self.errors) == MAX_RETAINED_CHAIN_ERRORS:
            self.errors.append(
                f"… further chain errors suppressed after "
                f"{MAX_RETAINED_CHAIN_ERRORS} (count still exact)"
            )

    def reset(self, reason: str) -> None:
        """Restart the chain walk (e.g. after log rotation).

        Honest note: continuity *across* the rotation boundary is not
        verified — we no longer hold the predecessor's recomputed hash. The
        reason is recorded so the operator can see the gap rather than
        mistake it for an unbroken chain.
        """
        if self._enabled:
            self._record_error(f"chain restarted: {reason} (continuity not verified)")
        self._prev_recomputed = None

    def consume(self, raw_line: str, lineno: int) -> dict[str, Any] | None:
        """Parse one raw line, advancing the chain. ``None`` = skip."""
        stripped = raw_line.rstrip("\n")
        if not stripped:
            return None
        try:
            record: dict[str, Any] = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if self._enabled:
                self._record_error(f"line {lineno}: invalid JSON — {exc}")
            return None

        if self._enabled:
            stored_entry_hash = record.get("entry_hash", "")
            stored_prev_hash = record.get("prev_hash")
            if stored_prev_hash != self._prev_recomputed:
                self._record_error(
                    f"line {lineno}: prev_hash mismatch "
                    f"(stored={stored_prev_hash!r}, expected={self._prev_recomputed!r})"
                )
            d_no_hash = {k: v for k, v in record.items() if k != "entry_hash"}
            recomputed = _sha256(_canonical_json(d_no_hash))
            if stored_entry_hash != recomputed:
                self._record_error(
                    f"line {lineno}: entry_hash mismatch "
                    f"(stored={stored_entry_hash!r}, recomputed={recomputed!r})"
                )
            self._prev_recomputed = recomputed

        return record


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


def _render_header(source: str, fmt: str) -> str:
    """The manifest line, in the stream's own format.

    JSON formats get the header object as a JSON line; ``cef`` gets it as a
    real CEF event so the stream stays pure CEF and a collector needs no
    special case for line 1.
    """
    manifest = _header(source, fmt)["nova_siem_export"]
    if fmt != "cef":
        return json.dumps({"nova_siem_export": manifest}, separators=(",", ":"))
    return _cef_line(
        "nova:siem.export.manifest",
        "SIEM export manifest",
        [
            ("rt", manifest["generated_at"]),
            ("cs1Label", "source"),
            ("cs1", manifest["source"]),
            ("cs2Label", "redactionRuleset"),
            ("cs2", manifest["redaction_ruleset"]),
            ("cs3Label", "fieldAllowlist"),
            ("cs3", manifest["field_allowlist"]),
            ("cs4Label", "adr"),
            ("cs4", manifest["adr"]),
        ],
    )


def export_entries(
    *,
    source: str,
    fmt: str,
    out: LineSink,
    path: Path | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> ExportResult:
    """Export *source* over ``[since, until)`` to *out*, one record per line.

    Writes the header entry first, then every in-window record after
    redaction. For the chained source the full file is walked (window or
    not) so chain verification covers the whole log; errors are returned,
    not raised — the caller distinguishes them via the exit code (D1/D5).
    """
    _check_source(source)
    if fmt not in ("jsonl", "ocsf", "cef"):
        raise SiemExportError(f"unknown format: {fmt!r} (expected jsonl|ocsf|cef)")

    src_path = path if path is not None else default_source_path(source)
    ts_field = _TIMESTAMP_FIELD[source]

    out.write(_render_header(source, fmt) + "\n")

    exported = 0
    verifier = _ChainVerifier(enabled=source == "audit")

    if not src_path.exists():
        return ExportResult(entries_exported=0, chain_errors=verifier.errors)

    with src_path.open("r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            record = verifier.consume(raw_line, lineno)
            if record is None:
                continue
            if not _in_window(_parse_ts(record.get(ts_field)), since, until):
                continue
            out.write(render_record(record, source=source, fmt=fmt) + "\n")
            exported += 1

    return ExportResult(entries_exported=exported, chain_errors=verifier.errors)


# --- follow mode (ADR-0191 D3, stdout sink) -----------------------------------


@dataclass(frozen=True)
class FollowResult:
    """Outcome of a follow session (returned when the loop is stopped)."""

    entries_emitted: int
    chain_errors: list[str] = field(default_factory=list)
    chain_error_count: int = 0
    rotations: int = 0


class _TailReader:
    """Rotation-aware line reader over a growing file.

    Handles the three things a naive ``readline`` loop gets wrong:

    - **rotation** — the path is re-stat'd each cycle; if its identity
      ``(st_dev, st_ino)`` no longer matches the open handle, the old handle
      is drained to EOF *first* (so entries written just before the rename
      are not lost) and only then reopened;
    - **truncation** — a file shorter than our offset means it was truncated
      in place (``copytruncate``-style rotation), so we reopen from 0 rather
      than sitting past EOF forever. Honest limit: a truncate that is
      refilled *past* the old offset within a single poll interval is
      indistinguishable from a plain append by size alone, and those entries
      would be missed. Rename-based rotation (the common case, and what
      ``logrotate`` does by default) has no such ambiguity;
    - **partial lines** — a line without a trailing newline is a half-written
      record; it is held in a buffer and completed on a later cycle instead
      of being parsed as corrupt.

    A file that does not exist yet is not an error: the reader waits for it,
    so a tailer can be started before the first audit event is ever written.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: TextIO | None = None
        self._identity: tuple[int, int] | None = None
        self._pending = ""
        self.lineno = 0
        self.rotations = 0

    def _identity_of(self, path: Path) -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def _open(self, *, from_start: bool) -> None:
        try:
            fh = self._path.open("r", encoding="utf-8")
        except OSError:
            return
        if not from_start:
            fh.seek(0, 2)  # tail semantics: start at EOF
        self._fh = fh
        self._identity = self._identity_of(self._path)
        self._pending = ""
        self.lineno = 0

    def open_initial(self, *, from_start: bool) -> None:
        self._open(from_start=from_start)

    def _drain(self) -> list[str]:
        """Read every complete line currently available from the open handle."""
        if self._fh is None:
            return []
        lines: list[str] = []
        while True:
            chunk = self._fh.readline()
            if not chunk:
                break
            self._pending += chunk
            if self._pending.endswith("\n"):
                lines.append(self._pending)
                self._pending = ""
                self.lineno += 1
        return lines

    def read_available(self) -> tuple[list[str], bool]:
        """Return ``(complete_lines, rotated)`` for this poll cycle."""
        if self._fh is None:
            self._open(from_start=True)  # file appeared after we started
            if self._fh is None:
                return [], False

        lines = self._drain()
        rotated = False

        current = self._identity_of(self._path)
        if current is not None and current != self._identity:
            # Renamed/replaced: drain the old handle, then follow the new file.
            lines += self._drain()
            self.close()
            self._open(from_start=True)
            self.rotations += 1
            rotated = True
        else:
            try:
                size = self._path.stat().st_size
                if self._fh is not None and size < self._fh.tell():
                    self.close()
                    self._open(from_start=True)
                    self.rotations += 1
                    rotated = True
                    lines += self._drain()
            except OSError:
                pass

        return lines, rotated

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def follow_entries(
    *,
    source: str,
    fmt: str,
    out: LineSink,
    path: Path | None = None,
    from_start: bool = False,
    poll_interval: float = 1.0,
    stop: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> FollowResult:
    """Continuously render new entries from *source* to *out* (ADR-0191 D3).

    A foreground loop the operator runs (systemd unit, sidecar) — **not** a
    NovaFabric-managed daemon, and there is no network sink: this slice
    writes to the caller's stream only, so ``… | your-shipper`` is the
    integration point. The rotating-file and RFC 5424 local-syslog sinks in
    D3 are a later slice.

    By default it starts at EOF (``tail`` semantics); ``from_start=True``
    replays the existing file first. Redaction, rendering and chain
    verification are the same code paths as the one-shot export.

    *stop* is polled each cycle so a caller (or a test) can end the loop;
    *sleep* is injectable for the same reason. Both default to "run until
    interrupted".
    """
    _check_source(source)
    if fmt not in ("jsonl", "ocsf", "cef"):
        raise SiemExportError(f"unknown format: {fmt!r} (expected jsonl|ocsf|cef)")
    if poll_interval <= 0:
        raise SiemExportError(
            f"poll interval must be positive (got {poll_interval!r})"
        )

    src_path = path if path is not None else default_source_path(source)
    verifier = _ChainVerifier(enabled=source == "audit")

    out.write(_render_header(source, fmt) + "\n")
    out.flush()

    reader = _TailReader(src_path)
    reader.open_initial(from_start=from_start)
    emitted = 0

    try:
        while stop is None or not stop():
            lines, rotated = reader.read_available()
            if rotated:
                verifier.reset(f"{src_path.name} rotated or truncated")
            for raw_line in lines:
                record = verifier.consume(raw_line, reader.lineno)
                if record is None:
                    continue
                out.write(render_record(record, source=source, fmt=fmt) + "\n")
                emitted += 1
            if lines:
                out.flush()  # a stalled pipe must not hide fresh events
            else:
                sleep(poll_interval)
    except KeyboardInterrupt:  # pragma: no cover - operator Ctrl-C
        pass
    finally:
        reader.close()

    return FollowResult(
        entries_emitted=emitted,
        chain_errors=verifier.errors,
        chain_error_count=verifier.error_count,
        rotations=reader.rotations,
    )
