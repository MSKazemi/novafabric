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
from typer.testing import CliRunner

from novafabric.audit import AuditEventType, AuditLog
from novafabric.audit.siem_export import (
    OCSF_CLASS_MAP,
    ExportResult,
    export_entries,
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
    def _no_socket(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("export path attempted network I/O (ADR-0191 D6)")

    monkeypatch.setattr(socket, "socket", _no_socket)
    for source, path in (("audit", audit_path), ("dashboard", dashboard_path)):
        for fmt in ("jsonl", "ocsf"):
            buf = io.StringIO()
            result = export_entries(source=source, fmt=fmt, out=buf, path=path)
            assert result.entries_exported > 0


# ---------------------------------------------------------------------------
# CLI (nova audit-log export)
# ---------------------------------------------------------------------------


def test_cli_help() -> None:
    result = runner.invoke(app, ["audit-log", "export", "--help"])
    assert result.exit_code == 0
    assert "--source" in result.output
    assert "--format" in result.output


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
    result = runner.invoke(app, ["audit-log", "export", "--format", "cef"])
    assert result.exit_code != 0


def test_cli_missing_source_file_is_empty_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novafabric.audit._paths.AUDIT_LOG_PATH", tmp_path / "does-not-exist.jsonl"
    )
    result = runner.invoke(app, ["audit-log", "export", "--source", "audit"])
    assert result.exit_code == 0, result.output
