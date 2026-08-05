"""ADR-0191 first slice: nova audit-log export (SIEM egress).

Covers: the OCSF mapping corpus (every audit event type must have a mapping —
a new type without one fails here); golden JSONL + OCSF renderings for both
sources; deny-by-default redaction (a seeded secret never appears in output);
chain-verification failure surfacing as exit code 3 while still exporting;
window filtering; the stdlib-only / no-socket proof; and CLI smoke.
"""

from __future__ import annotations

import io
import json
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.audit import AuditEventType, AuditLog
from novafabric.audit.siem_export import (
    KNOWN_SOURCES,
    MAX_RETAINED_CHAIN_ERRORS,
    OCSF_CLASS_MAP,
    ExportResult,
    SiemExportError,
    export_entries,
    follow_entries,
    redact_record,
    to_cef,
    to_ocsf,
)
from novafabric.cli.main import app

runner = CliRunner()

SECRET = "sk-ant-api03-verysecretvalue0123456789"
TOKEN = "ghp_seededtokenvalueABCDEFGH"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def audit_path(tmp_path: Path) -> Path:
    """A hash-chained audit log with 4 event types, incl. seeded secrets."""
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(
        AuditEventType.POLICY_DENY,
        actor="alice",
        resource_id="asset-a",
        details={"api_token": TOKEN, "reason": "gate failed"},
    )
    log.append(
        AuditEventType.PROMOTE,
        actor="bob",
        resource_id="asset-a",
        details={"stage": "prod", "note": f"password: {SECRET}"},
    )
    log.append(
        AuditEventType.CAPSULE_DELETE,
        actor="carol",
        resource_id="run-001",
        details={},
    )
    log.append(
        AuditEventType.EVIDENCE_EXPORT,
        actor="dave",
        resource_id="run-002",
        details={"dest": "bundle.tar.gz"},
    )
    return path


@pytest.fixture()
def dashboard_path(tmp_path: Path) -> Path:
    """A dashboard mutation log (ADR-0027 Layer B shape), incl. a secret."""
    path = tmp_path / "dashboard-audit.jsonl"
    records = [
        {
            "audit_id": "11111111-1111-1111-1111-111111111111",
            "ts": "2026-07-10T10:00:00+00:00",
            "action": "retention.apply",
            "args": {"policy": "p1", "secret_key": SECRET},
            "cli_equivalent": "nova retention apply p1",
            "actor_token_fp": "abcd1234",
            "result": "ok",
            "wild_field": "must-not-leave",
        },
        {
            "audit_id": "22222222-2222-2222-2222-222222222222",
            "ts": "2026-07-11T11:00:00+00:00",
            "action": "session.revoke",
            "args": {},
            "cli_equivalent": "nova serve",
            "actor_token_fp": "abcd1234",
            "result": "error",
            "error": f"token={TOKEN} rejected",
        },
    ]
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return path


def _export(source: str, fmt: str, path: Path, **kw: Any) -> tuple[ExportResult, list[dict[str, Any]]]:
    buf = io.StringIO()
    result = export_entries(source=source, fmt=fmt, out=buf, path=path, **kw)
    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line]
    return result, lines


# ---------------------------------------------------------------------------
# OCSF mapping corpus — a new event type without a mapping fails CI
# ---------------------------------------------------------------------------


def test_every_audit_event_type_has_an_ocsf_mapping() -> None:
    unmapped = [et.value for et in AuditEventType if et.value not in OCSF_CLASS_MAP]
    assert not unmapped, (
        f"AuditEventType member(s) without an OCSF mapping: {unmapped}. "
        "Add them to OCSF_CLASS_MAP in siem_export.py AND to the mapping "
        "table in design/spec/audit-siem-egress-v0.md (ADR-0191 D2)."
    )


def test_ocsf_mapping_uses_only_documented_classes() -> None:
    for class_uid, _name, _activity in OCSF_CLASS_MAP.values():
        assert class_uid in (3002, 6002, 6003)


# ---------------------------------------------------------------------------
# Golden renderings — audit (chained) source
# ---------------------------------------------------------------------------


def test_jsonl_export_audit_header_and_verbatim_hashes(audit_path: Path) -> None:
    result, lines = _export("audit", "jsonl", audit_path)
    assert result.entries_exported == 4
    assert result.chain_errors == []

    header = lines[0]["nova_siem_export"]
    assert header["source"] == "audit"
    assert header["format"] == "jsonl"
    assert "adr0187" in header["redaction_ruleset"]

    entries = lines[1:]
    assert len(entries) == 4
    for entry in entries:
        # D5: chain hashes travel verbatim in JSONL.
        assert entry["entry_hash"]
        assert "prev_hash" in entry
    assert entries[0]["event_type"] == "policy.deny"
    assert entries[1]["event_type"] == "promote"
    assert entries[2]["event_type"] == "capsule.delete"


def test_ocsf_export_audit_classes_and_unmapped(audit_path: Path) -> None:
    result, lines = _export("audit", "ocsf", audit_path)
    assert result.entries_exported == 4
    events = lines[1:]

    by_type = {e["unmapped"]["event_type"]: e for e in events}
    # Golden class pins for 3+ event types (ADR-0191 D2).
    assert by_type["policy.deny"]["class_uid"] == 6003
    assert by_type["promote"]["class_uid"] == 6002
    assert by_type["capsule.delete"]["class_uid"] == 6003
    assert by_type["evidence.export"]["class_uid"] == 6003

    for event in events:
        assert event["type_uid"] == event["class_uid"] * 100 + event["activity_id"]
        assert event["metadata"]["product"]["name"] == "NovaFabric"
        assert isinstance(event["time"], int)
        # D5: chain hashes ride in unmapped for OCSF.
        assert event["unmapped"]["entry_hash"]
        assert "prev_hash" in event["unmapped"]

    assert by_type["promote"]["actor"]["user"]["name"] == "bob"


def test_to_ocsf_unknown_event_type_is_conservative() -> None:
    record = {
        "timestamp": "2026-07-10T10:00:00+00:00",
        "event_type": "weird.new-type",
        "actor": "eve",
        "resource_id": "x",
        "details": {},
        "prev_hash": None,
        "entry_hash": "deadbeef",
    }
    event = to_ocsf(record, source="audit")
    assert event["class_uid"] == 6003  # conservative default: API Activity
    assert event["unmapped"]["event_type"] == "weird.new-type"


# ---------------------------------------------------------------------------
# Golden renderings — dashboard source
# ---------------------------------------------------------------------------


def test_jsonl_export_dashboard_allowlist(dashboard_path: Path) -> None:
    result, lines = _export("dashboard", "jsonl", dashboard_path)
    assert result.entries_exported == 2
    assert result.chain_errors == []

    entries = lines[1:]
    assert entries[0]["action"] == "retention.apply"
    assert entries[0]["actor_token_fp"] == "abcd1234"
    # Deny-by-default: a field outside the allowlist never leaves.
    assert "wild_field" not in entries[0]


def test_ocsf_export_dashboard_class(dashboard_path: Path) -> None:
    _result, lines = _export("dashboard", "ocsf", dashboard_path)
    events = lines[1:]
    assert events[0]["class_uid"] == 6003
    assert events[1]["class_uid"] == 3002  # session.* → Authentication
    assert events[0]["actor"]["user"]["name"] == "abcd1234"
    assert events[0]["unmapped"]["cli_equivalent"] == "nova retention apply p1"


# ---------------------------------------------------------------------------
# Deny-by-default redaction (ADR-0191 D4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["audit", "dashboard"])
@pytest.mark.parametrize("fmt", ["jsonl", "ocsf"])
def test_seeded_secret_never_appears_in_output(
    source: str, fmt: str, audit_path: Path, dashboard_path: Path
) -> None:
    path = audit_path if source == "audit" else dashboard_path
    buf = io.StringIO()
    export_entries(source=source, fmt=fmt, out=buf, path=path)
    text = buf.getvalue()
    assert SECRET not in text
    assert TOKEN not in text
    assert "[REDACTED]" in text


def test_redaction_ruleset_version_in_header(audit_path: Path) -> None:
    _result, lines = _export("audit", "jsonl", audit_path)
    header = lines[0]["nova_siem_export"]
    # Both halves of the pipeline are versioned in the header (D4).
    assert header["redaction_ruleset"].startswith("adr0187-")
    assert header["field_allowlist"].startswith("siem-allowlist-")


# ---------------------------------------------------------------------------
# Chain verification (ADR-0191 D5)
# ---------------------------------------------------------------------------


def test_chain_tamper_is_detected_but_export_still_writes(audit_path: Path) -> None:
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[1])
    tampered["actor"] = "mallory"  # edit without recomputing entry_hash
    lines[1] = json.dumps(tampered, separators=(",", ":"), sort_keys=True)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result, out_lines = _export("audit", "jsonl", audit_path)
    assert result.chain_errors  # tamper evidence surfaced
    assert result.entries_exported == 4  # still writes what it exported
    assert len(out_lines) == 1 + 4


def test_intact_chain_has_no_errors(audit_path: Path) -> None:
    result, _lines = _export("audit", "jsonl", audit_path)
    assert result.chain_errors == []


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------


def test_window_filtering_dashboard(dashboard_path: Path) -> None:
    since = datetime(2026, 7, 11, tzinfo=timezone.utc)
    result, lines = _export("dashboard", "jsonl", dashboard_path, since=since)
    assert result.entries_exported == 1
    assert lines[1]["action"] == "session.revoke"

    until = datetime(2026, 7, 11, tzinfo=timezone.utc)  # exclusive
    result, lines = _export("dashboard", "jsonl", dashboard_path, until=until)
    assert result.entries_exported == 1
    assert lines[1]["action"] == "retention.apply"


def test_window_filtering_audit_all_out(audit_path: Path) -> None:
    until = datetime(2000, 1, 1, tzinfo=timezone.utc)
    result, lines = _export("audit", "jsonl", audit_path, until=until)
    assert result.entries_exported == 0
    assert len(lines) == 1  # header only
    # The chain is still walked in full even when the window excludes all.
    assert result.chain_errors == []


# ---------------------------------------------------------------------------
# No network I/O in the export path (ADR-0191 D6)
# ---------------------------------------------------------------------------


def test_export_path_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch, audit_path: Path, dashboard_path: Path
) -> None:
    """No socket on the default paths (ADR-0191 D6).

    Scope note: since the D3 syslog sink landed, "this module never opens a
    socket" is **conditional**, not absolute — a socket is opened only when
    an operator explicitly configures `--syslog`, and only to a Unix socket
    or loopback address (enforced in `novafabric.audit.sinks`). This test
    pins the part that must stay absolute: `export`, and `tail` to stdout or
    a file, do no network I/O at all.
    """

    def _no_socket(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("export path attempted network I/O (ADR-0191 D6)")

    monkeypatch.setattr(socket, "socket", _no_socket)
    for source, path in (("audit", audit_path), ("dashboard", dashboard_path)):
        for fmt in ("jsonl", "ocsf", "cef"):
            buf = io.StringIO()
            result = export_entries(source=source, fmt=fmt, out=buf, path=path)
            assert result.entries_exported > 0


# ---------------------------------------------------------------------------
# D4 side channel — a bare credential in free text must not leave
# ---------------------------------------------------------------------------


def test_bare_credential_in_details_never_leaves_in_any_format(
    tmp_path: Path,
) -> None:
    """ADR-0191 D4: the audit log must not export what capsules redact.

    Key-driven redaction alone missed this — nothing here *names* the value
    as a secret, it is just prose containing a token (ruleset v3).
    """
    path = tmp_path / "bare.jsonl"
    record = {
        "audit_id": "a1",
        "ts": "2026-07-12T12:00:00+00:00",
        "action": "provider.call",
        "args": {"note": f"upstream rejected {SECRET}"},
        "actor_token_fp": "fp",
        "result": "error",
        "error": f"bad credential {TOKEN} seen in response",
    }
    path.write_text(json.dumps(record, separators=(",", ":")) + "\n", encoding="utf-8")

    for fmt in ("jsonl", "ocsf", "cef"):
        _result, lines = (
            _export_cef("dashboard", path)
            if fmt == "cef"
            else _export("dashboard", fmt, path)
        )
        blob = "\n".join(lines) if fmt == "cef" else json.dumps(lines)
        assert SECRET not in blob, fmt
        assert TOKEN not in blob, fmt
        assert "[REDACTED]" in blob, fmt


def test_integrity_hashes_survive_value_redaction(audit_path: Path) -> None:
    """The other half of D4/D5: redaction must not eat the chain hashes."""
    _result, lines = _export("audit", "jsonl", audit_path)
    entries = [line for line in lines if "nova_siem_export" not in line]
    for entry in entries:
        assert len(entry["entry_hash"]) == 64
        assert "REDACTED" not in entry["entry_hash"]
        if entry["prev_hash"] is not None:
            assert "REDACTED" not in entry["prev_hash"]


# ---------------------------------------------------------------------------
# Source registry — an unregistered source must fail LOUDLY, never fall back
# ---------------------------------------------------------------------------


def test_every_known_source_is_fully_registered() -> None:
    """A source is only exportable once all three per-source tables cover it.

    Guards the failure mode this registry replaced: a source added to one
    table but not the allowlist silently inherited another source's
    allowlist, letting unreviewed fields leave (ADR-0191 D4).
    """
    from novafabric.audit.siem_export import _CEF_CONSUMED, _TIMESTAMP_FIELD

    for source in KNOWN_SOURCES:
        assert source in _TIMESTAMP_FIELD, f"{source} missing a timestamp field"
        assert source in _CEF_CONSUMED, f"{source} missing a CEF field partition"


@pytest.mark.parametrize("source", ["server", "", "AUDIT", "unknown"])
def test_unregistered_source_is_rejected_not_silently_downgraded(
    source: str, dashboard_path: Path
) -> None:
    """Redaction must never fall back to another source's allowlist."""
    with pytest.raises(SiemExportError, match="unknown audit source"):
        redact_record({"audit_id": "x", "wild_field": "leak"}, source)
    with pytest.raises(SiemExportError, match="unknown audit source"):
        to_ocsf({"ts": "2026-07-10T10:00:00+00:00"}, source=source)
    with pytest.raises(SiemExportError, match="unknown audit source"):
        to_cef({"ts": "2026-07-10T10:00:00+00:00"}, source=source)
    with pytest.raises(SiemExportError, match="unknown audit source"):
        export_entries(
            source=source, fmt="jsonl", out=io.StringIO(), path=dashboard_path
        )


def test_rejection_message_lists_the_supported_sources() -> None:
    with pytest.raises(SiemExportError, match="audit|dashboard"):
        redact_record({}, "server")


# ---------------------------------------------------------------------------
# CEF rendering (ADR-0191 D2 slice 2)
# ---------------------------------------------------------------------------


def _export_cef(source: str, path: Path, **kw: Any) -> tuple[ExportResult, list[str]]:
    buf = io.StringIO()
    result = export_entries(source=source, fmt="cef", out=buf, path=path, **kw)
    return result, [line for line in buf.getvalue().splitlines() if line]


def _cef_parts(line: str) -> tuple[list[str], dict[str, str]]:
    """Split a CEF line into its 7 header fields and its extension dict.

    Splits on *unescaped* separators only, so this doubles as an escaping
    check: a naive ``str.split`` would mis-parse a payload containing ``|``.
    """
    header, rest = [], []
    buf, escaped, in_ext = "", False, False
    for ch in line:
        if in_ext:
            rest.append(ch)
            continue
        if escaped:
            buf += ch  # unescape: the backslash itself is dropped
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "|":
            header.append(buf)
            buf = ""
            if len(header) == 7:
                in_ext = True
        else:
            buf += ch
    if len(header) < 7:
        header.append(buf)

    ext: dict[str, str] = {}
    key, value, escaped = "", "", False
    tokens = "".join(rest)
    # Extension is `k=v k=v`; a value may contain spaces, so a new pair starts
    # only at the last space before an unescaped '='.
    pairs: list[str] = []
    current = ""
    i = 0
    while i < len(tokens):
        ch = tokens[i]
        if ch == "\\" and i + 1 < len(tokens):
            current += tokens[i : i + 2]
            i += 2
            continue
        if ch == " ":
            remainder = tokens[i + 1 :]
            eq = remainder.find("=")
            sp = remainder.find(" ")
            if eq != -1 and (sp == -1 or eq < sp):
                pairs.append(current)
                current = ""
                i += 1
                continue
        current += ch
        i += 1
    if current:
        pairs.append(current)
    for pair in pairs:
        key, _, value = pair.partition("=")
        ext[key] = value
    return header, ext


def test_cef_header_shape_and_manifest_line(audit_path: Path) -> None:
    result, lines = _export_cef("audit", audit_path)
    assert result.entries_exported == 4
    assert len(lines) == 1 + 4

    # Line 1 is a real CEF event, not JSON — the stream is pure CEF.
    header, ext = _cef_parts(lines[0])
    assert header[0] == "CEF:0"
    assert header[1] == "NovaFabric"
    assert header[4] == "nova:siem.export.manifest"
    assert ext["cs1"] == "audit"
    assert ext["cs2"].startswith("adr0187-")
    assert ext["cs3"].startswith("siem-allowlist-")

    for line in lines:
        assert line.startswith("CEF:0|")
        assert len(_cef_parts(line)[0]) == 7


def test_cef_event_maps_hashes_and_keeps_native_signature(audit_path: Path) -> None:
    _result, lines = _export_cef("audit", audit_path)
    header, ext = _cef_parts(lines[1])

    # Signature id keeps the NATIVE taxonomy; name carries the OCSF class.
    assert header[4] == "policy.deny"
    assert header[5] == "API Activity: policy.deny"
    assert header[6] == "3"  # constant severity — SIEM decides alarm level

    # D5: chain hashes survive into labelled custom strings.
    assert ext["cs1Label"] == "entryHash"
    assert len(ext["cs1"]) == 64
    assert ext["cs2Label"] == "prevHash"
    assert ext["cs3"] == "asset-a"
    assert ext["suser"] == "alice"
    assert ext["rt"].isdigit()
    assert ext["cs5Label"] == "ocsfClassUid"
    assert ext["cs5"] == "6003"


def test_cef_and_ocsf_agree_on_class_for_every_event_type() -> None:
    """The two formats must never disagree about what an event *is*."""
    for event_type in OCSF_CLASS_MAP:
        record = {"event_type": event_type, "timestamp": "2026-07-10T10:00:00+00:00"}
        ocsf_class = to_ocsf(record, source="audit")["class_uid"]
        _header, ext = _cef_parts(to_cef(record, source="audit"))
        assert ext["cs5"] == str(ocsf_class), event_type


def test_cef_packs_remaining_fields_into_cs6_without_loss(audit_path: Path) -> None:
    _result, lines = _export_cef("audit", audit_path)
    _header, ext = _cef_parts(lines[4])  # evidence.export, has details
    assert ext["cs6Label"] == "novaUnmapped"
    # cs6 is compact JSON of everything not consumed into a named key.
    unmapped = json.loads(ext["cs6"].replace("\\=", "="))
    assert unmapped["details"]["dest"] == "bundle.tar.gz"


def test_cef_escapes_separators_in_payload(tmp_path: Path) -> None:
    """A payload full of CEF metacharacters must not break the framing."""
    path = tmp_path / "dash.jsonl"
    path.write_text(
        json.dumps(
            {
                "audit_id": "id|with|pipes",
                "ts": "2026-07-10T10:00:00+00:00",
                "action": "retention|apply\\weird",
                "actor_token_fp": "fp",
                "result": "ok",
                "extra": {"note": "a=b|c\\d\nsecond line"},
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    _result, lines = _export_cef("dashboard", path)
    event = lines[1]
    assert "\n" not in event  # newline escaped, framing intact
    # Header fields escape '|' and '\' so the 7-field framing survives a
    # payload that contains both; the raw escapes are visible in the line.
    assert "retention\\|apply\\\\weird" in event
    header, ext = _cef_parts(event)
    assert len(header) == 7
    assert header[4] == "retention|apply\\weird"  # round-trips after unescaping
    # '|' is only a *header* separator — in the extension it needs no escape.
    assert ext["externalId"] == "id|with|pipes"
    assert "\\n" in ext["cs6"]  # newline escaped inside the JSON payload
    assert "\\=" in ext["cs6"]  # '=' escaped so pair-splitting stays correct


def test_cef_omits_empty_extension_keys() -> None:
    line = to_cef(
        {"event_type": "promote", "timestamp": "2026-07-10T10:00:00+00:00"},
        source="audit",
    )
    _header, ext = _cef_parts(line)
    assert "cs1" not in ext  # no entry_hash on this record
    assert "suser" not in ext  # no actor
    assert ext["cs5"] == "6002"


def test_cef_dashboard_source_maps_outcome_and_cli(dashboard_path: Path) -> None:
    _result, lines = _export_cef("dashboard", dashboard_path)
    header, ext = _cef_parts(lines[2])  # session.revoke → Authentication
    assert header[5] == "Authentication: session.revoke"
    assert ext["outcome"] == "error"
    assert ext["cs1Label"] == "cliEquivalent"
    assert ext["cs5"] == "3002"


def test_cef_export_redacts_seeded_secrets(
    audit_path: Path, dashboard_path: Path
) -> None:
    for source, path in (("audit", audit_path), ("dashboard", dashboard_path)):
        _result, lines = _export_cef(source, path)
        blob = "\n".join(lines)
        assert SECRET not in blob
        assert TOKEN not in blob


def test_cef_export_still_verifies_the_chain(audit_path: Path) -> None:
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["actor"] = "mallory"
    lines[0] = json.dumps(tampered, separators=(",", ":"), sort_keys=True)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result, out_lines = _export_cef("audit", audit_path)
    assert result.chain_errors
    assert len(out_lines) == 1 + 4  # tamper evidence still exported


def test_cef_window_filtering(dashboard_path: Path) -> None:
    result, lines = _export_cef(
        "dashboard",
        dashboard_path,
        since=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )
    assert result.entries_exported == 1
    assert "session.revoke" in lines[1]


# ---------------------------------------------------------------------------
# Follow mode (ADR-0191 D3, stdout sink)
# ---------------------------------------------------------------------------


def _stop_after(n: int) -> Any:
    """A ``stop`` predicate that ends the follow loop after *n* cycles."""
    calls = 0

    def _stop() -> bool:
        nonlocal calls
        calls += 1
        return calls > n

    return _stop


def _append_dashboard(path: Path, action: str, **extra: Any) -> None:
    record = {
        "audit_id": f"id-{action}",
        "ts": "2026-07-12T12:00:00+00:00",
        "action": action,
        "args": {},
        "actor_token_fp": "fp",
        "result": "ok",
        **extra,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def test_follow_starts_at_eof_by_default(dashboard_path: Path) -> None:
    """Default is tail semantics: pre-existing entries are NOT replayed."""
    buf = io.StringIO()
    result = follow_entries(
        source="dashboard",
        fmt="jsonl",
        out=buf,
        path=dashboard_path,
        stop=_stop_after(1),
        sleep=lambda _s: None,
    )
    assert result.entries_emitted == 0
    assert "retention.apply" not in buf.getvalue()
    # The manifest is still emitted, so a consumer always sees the ruleset.
    assert "nova_siem_export" in buf.getvalue()


def test_follow_from_start_replays_then_follows(dashboard_path: Path) -> None:
    buf = io.StringIO()
    result = follow_entries(
        source="dashboard",
        fmt="jsonl",
        out=buf,
        path=dashboard_path,
        from_start=True,
        stop=_stop_after(1),
        sleep=lambda _s: None,
    )
    assert result.entries_emitted == 2
    assert "retention.apply" in buf.getvalue()


def test_follow_picks_up_appended_entries(dashboard_path: Path) -> None:
    """New lines written while following are rendered."""
    appended = {"done": False}

    def _sleep(_seconds: float) -> None:
        if not appended["done"]:
            _append_dashboard(dashboard_path, "late.event")
            appended["done"] = True

    buf = io.StringIO()
    result = follow_entries(
        source="dashboard",
        fmt="jsonl",
        out=buf,
        path=dashboard_path,
        stop=_stop_after(4),
        sleep=_sleep,
    )
    assert result.entries_emitted == 1
    assert "late.event" in buf.getvalue()


def test_follow_holds_partial_lines_until_complete(tmp_path: Path) -> None:
    """A half-written record must not be parsed as corrupt."""
    path = tmp_path / "partial.jsonl"
    path.write_text("", encoding="utf-8")
    record = json.dumps(
        {
            "audit_id": "x",
            "ts": "2026-07-12T12:00:00+00:00",
            "action": "slow.write",
            "actor_token_fp": "fp",
            "result": "ok",
        },
        separators=(",", ":"),
    )
    state = {"cycle": 0}

    def _sleep(_seconds: float) -> None:
        state["cycle"] += 1
        with path.open("a", encoding="utf-8") as fh:
            if state["cycle"] == 1:
                fh.write(record[:12])  # torn write, no newline
            elif state["cycle"] == 2:
                fh.write(record[12:] + "\n")  # completed

    buf = io.StringIO()
    result = follow_entries(
        source="dashboard",
        fmt="jsonl",
        out=buf,
        path=path,
        stop=_stop_after(5),
        sleep=_sleep,
    )
    assert result.entries_emitted == 1
    assert "slow.write" in buf.getvalue()
    assert result.chain_error_count == 0  # never seen as invalid JSON


def test_follow_detects_rotation_and_drains_the_old_file(tmp_path: Path) -> None:
    """Entries written just before a rename are not lost."""
    path = tmp_path / "rot.jsonl"
    _append_dashboard(path, "before.rotation")
    state = {"cycle": 0}

    def _sleep(_seconds: float) -> None:
        state["cycle"] += 1
        if state["cycle"] == 1:
            # Write one more, THEN rotate — the drain must catch it.
            _append_dashboard(path, "just.before.rename")
            path.rename(tmp_path / "rot.jsonl.1")
            _append_dashboard(path, "after.rotation")

    buf = io.StringIO()
    result = follow_entries(
        source="dashboard",
        fmt="jsonl",
        out=buf,
        path=path,
        from_start=True,
        stop=_stop_after(5),
        sleep=_sleep,
    )
    output = buf.getvalue()
    assert result.rotations == 1
    assert "before.rotation" in output
    assert "just.before.rename" in output, "pre-rotation entry was lost"
    assert "after.rotation" in output


def test_follow_detects_in_place_truncation(tmp_path: Path) -> None:
    path = tmp_path / "trunc.jsonl"
    _append_dashboard(path, "first.entry")
    state = {"cycle": 0}

    def _sleep(_seconds: float) -> None:
        # The real copytruncate sequence: truncate, and only later does the
        # writer refill. (A truncate refilled past the old size *within* one
        # poll interval is undetectable by size alone — documented limitation.)
        state["cycle"] += 1
        if state["cycle"] == 1:
            path.write_text("", encoding="utf-8")  # truncate in place
        elif state["cycle"] == 2:
            _append_dashboard(path, "post.truncate")

    buf = io.StringIO()
    result = follow_entries(
        source="dashboard",
        fmt="jsonl",
        out=buf,
        path=path,
        from_start=True,
        stop=_stop_after(5),
        sleep=_sleep,
    )
    assert result.rotations == 1
    assert "post.truncate" in buf.getvalue()


def test_follow_waits_for_a_log_that_does_not_exist_yet(tmp_path: Path) -> None:
    """A tailer may legitimately start before the first audit event."""
    path = tmp_path / "not-yet.jsonl"
    state = {"cycle": 0}

    def _sleep(_seconds: float) -> None:
        state["cycle"] += 1
        if state["cycle"] == 1:
            _append_dashboard(path, "first.ever")

    buf = io.StringIO()
    result = follow_entries(
        source="dashboard",
        fmt="jsonl",
        out=buf,
        path=path,
        stop=_stop_after(5),
        sleep=_sleep,
    )
    assert result.entries_emitted == 1
    assert "first.ever" in buf.getvalue()


def test_follow_reports_chain_restart_rather_than_claiming_continuity(
    tmp_path: Path, audit_path: Path
) -> None:
    """Rotation breaks chain continuity — that gap must be visible."""
    state = {"cycle": 0}

    def _sleep(_seconds: float) -> None:
        state["cycle"] += 1
        if state["cycle"] == 1:
            audit_path.rename(tmp_path / "rotated-away.jsonl")
            audit_path.write_text("", encoding="utf-8")

    buf = io.StringIO()
    result = follow_entries(
        source="audit",
        fmt="jsonl",
        out=buf,
        path=audit_path,
        from_start=True,
        stop=_stop_after(4),
        sleep=_sleep,
    )
    assert result.rotations == 1
    assert any("chain restarted" in e for e in result.chain_errors)


def test_follow_chain_errors_are_bounded_but_counted(tmp_path: Path) -> None:
    """A long-running follow must not accumulate errors without bound."""
    path = tmp_path / "broken.jsonl"
    bad = json.dumps({"entry_id": "x", "entry_hash": "wrong", "prev_hash": None})
    with path.open("w", encoding="utf-8") as fh:
        for _ in range(MAX_RETAINED_CHAIN_ERRORS + 50):
            fh.write(bad + "\n")

    buf = io.StringIO()
    result = follow_entries(
        source="audit",
        fmt="jsonl",
        out=buf,
        path=path,
        from_start=True,
        stop=_stop_after(1),
        sleep=lambda _s: None,
    )
    assert len(result.chain_errors) <= MAX_RETAINED_CHAIN_ERRORS + 1
    assert result.chain_error_count > len(result.chain_errors)
    assert any("suppressed" in e for e in result.chain_errors)


def test_follow_applies_redaction_in_every_format(tmp_path: Path) -> None:
    path = tmp_path / "secret.jsonl"
    _append_dashboard(
        path, "leaky", args={"secret_key": SECRET}, error=f"token={TOKEN} rejected"
    )
    for fmt in ("jsonl", "ocsf", "cef"):
        buf = io.StringIO()
        follow_entries(
            source="dashboard",
            fmt=fmt,
            out=buf,
            path=path,
            from_start=True,
            stop=_stop_after(1),
            sleep=lambda _s: None,
        )
        assert SECRET not in buf.getvalue(), fmt
        assert TOKEN not in buf.getvalue(), fmt


def test_follow_rejects_bad_parameters(dashboard_path: Path) -> None:
    for kwargs in (
        {"source": "server", "fmt": "jsonl"},
        {"source": "dashboard", "fmt": "syslog"},
    ):
        with pytest.raises(SiemExportError):
            follow_entries(out=io.StringIO(), path=dashboard_path, **kwargs)
    with pytest.raises(SiemExportError, match="poll interval"):
        follow_entries(
            source="dashboard",
            fmt="jsonl",
            out=io.StringIO(),
            path=dashboard_path,
            poll_interval=0,
        )


def test_follow_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch, dashboard_path: Path
) -> None:
    def _no_socket(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("follow path attempted network I/O (ADR-0191 D3/D6)")

    monkeypatch.setattr(socket, "socket", _no_socket)
    buf = io.StringIO()
    result = follow_entries(
        source="dashboard",
        fmt="jsonl",
        out=buf,
        path=dashboard_path,
        from_start=True,
        stop=_stop_after(1),
        sleep=lambda _s: None,
    )
    assert result.entries_emitted == 2


# ---------------------------------------------------------------------------
# CLI (nova audit-log export)
# ---------------------------------------------------------------------------


def test_cli_help() -> None:
    result = runner.invoke(app, ["audit-log", "export", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--source")
    assert_flag_in_help(result, "--format")


def test_cli_export_audit_to_file(
    tmp_path: Path, audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("novafabric.audit._paths.AUDIT_LOG_PATH", audit_path)
    out = tmp_path / "export.jsonl"
    result = runner.invoke(
        app, ["audit-log", "export", "--source", "audit", "--format", "ocsf", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert "nova_siem_export" in lines[0]
    assert len(lines) == 1 + 4


def test_cli_export_audit_cef(
    tmp_path: Path, audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("novafabric.audit._paths.AUDIT_LOG_PATH", audit_path)
    out = tmp_path / "export.cef"
    result = runner.invoke(
        app,
        ["audit-log", "export", "--source", "audit", "--format", "cef", "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + 4
    assert all(line.startswith("CEF:0|NovaFabric|NovaFabric|") for line in lines)
    assert SECRET not in out.read_text(encoding="utf-8")


def test_cli_export_dashboard_stdout_default(
    dashboard_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(dashboard_path))
    result = runner.invoke(app, ["audit-log", "export", "--source", "dashboard"])
    assert result.exit_code == 0, result.output
    assert "retention.apply" in result.output
    assert SECRET not in result.output


def test_cli_window_flags(dashboard_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(dashboard_path))
    result = runner.invoke(
        app,
        [
            "audit-log", "export", "--source", "dashboard",
            "--since", "2026-07-11T00:00:00+00:00",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "session.revoke" in result.output
    assert "retention.apply" not in result.output


def test_cli_chain_tamper_exit_code_3(
    tmp_path: Path, audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["resource_id"] = "swapped"
    lines[0] = json.dumps(tampered, separators=(",", ":"), sort_keys=True)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setattr("novafabric.audit._paths.AUDIT_LOG_PATH", audit_path)
    out = tmp_path / "export.jsonl"
    result = runner.invoke(
        app, ["audit-log", "export", "--source", "audit", "--out", str(out)]
    )
    assert result.exit_code == 3
    # Still writes what it exported (ADR-0191 D1).
    assert len(out.read_text().splitlines()) == 1 + 4


def test_cli_rejects_unknown_source_and_format(tmp_path: Path) -> None:
    result = runner.invoke(app, ["audit-log", "export", "--source", "server"])
    assert result.exit_code != 0
    result = runner.invoke(app, ["audit-log", "export", "--format", "syslog"])
    assert result.exit_code != 0


def test_cli_tail_help() -> None:
    result = runner.invoke(app, ["audit-log", "tail", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--follow")
    assert_flag_in_help(result, "--from-start")


def test_cli_tail_without_follow_is_a_bounded_single_pass(
    dashboard_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No --follow must terminate, not block forever on an idle log."""
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(dashboard_path))
    result = runner.invoke(app, ["audit-log", "tail", "--source", "dashboard"])
    assert result.exit_code == 0, result.output
    assert "retention.apply" in result.output
    assert SECRET not in result.output


def test_cli_tail_rejects_bad_parameters(dashboard_path: Path) -> None:
    for args in (
        ["audit-log", "tail", "--source", "server"],
        ["audit-log", "tail", "--format", "syslog"],
    ):
        assert runner.invoke(app, args).exit_code != 0


def test_cli_tail_chain_tamper_exit_code_3(
    audit_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[0])
    tampered["actor"] = "mallory"
    lines[0] = json.dumps(tampered, separators=(",", ":"), sort_keys=True)
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    monkeypatch.setattr("novafabric.audit._paths.AUDIT_LOG_PATH", audit_path)
    result = runner.invoke(app, ["audit-log", "tail", "--source", "audit"])
    assert result.exit_code == 3


def test_cli_missing_source_file_is_empty_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novafabric.audit._paths.AUDIT_LOG_PATH", tmp_path / "does-not-exist.jsonl"
    )
    result = runner.invoke(app, ["audit-log", "export", "--source", "audit"])
    assert result.exit_code == 0, result.output
