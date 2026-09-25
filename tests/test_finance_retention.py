"""ADR-0159 D5 / NF-277 — retention-posture renderer and its read-only collectors.

Acceptance criteria covered:
- the RFC 3161 timestamp is reported **when the bundle carries one** (``complete``) and
  ``missing``-with-reason only when it genuinely was not obtained (never "always missing");
- a TSR whose digest does not match the manifest, or that the TSA did not grant, is ``partial``;
- WORM: cloud receipt -> complete; local dev adapter -> partial (not true WORM); expired lock or
  failed integrity -> partial; none -> missing;
- retention-policy / legal-hold posture read from the registry; absent registry -> missing;
- audit trail: entries -> complete; broken hash chain / unparseable line -> partial; none -> missing;
- corrupt sealed evidence raises ``CorruptEvidenceError``; no compliant/verdict field anywhere.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from novafabric.audit import AuditEventType, AuditLog
from novafabric.compliance.export.finance import retention_collect as rc
from novafabric.compliance.export.finance.retention import (
    LOCAL_WORM_REASON,
    REGIME_TEXT,
    AuditFacts,
    BundleFacts,
    Regime,
    TimestampFacts,
    WormFacts,
    audit_row,
    build_retention_attestation,
    hold_row,
    policy_row,
    timestamp_row,
    worm_row,
)
from novafabric.compliance.export.provenance import EvidenceSource
from novafabric.storage._local_worm import LocalWormAdapter

NOW = datetime(2026, 9, 1, tzinfo=UTC)
TSR_GRANTED = bytes([0x30, 0x05, 0x30, 0x03, 0x02, 0x01, 0x00])
TSR_REJECTED = bytes([0x30, 0x05, 0x30, 0x03, 0x02, 0x01, 0x02])
TSR_GARBAGE = bytes([0x04, 0x00])


def _bundle(
    tmp_path: Path,
    name: str = "b.zip",
    *,
    run_id: str = "run-1",
    tsr: bytes | None = None,
    manifest_extra: dict[str, object] | None = None,
) -> Path:
    manifest: dict[str, object] = {"bundle_id": "BND1", "subject": {"run_id": run_id}}
    if tsr is not None:
        manifest.update(
            timestamp_status="ok",
            timestamp_tsa_url="https://tsa.example",
            manifest_dsse_tsr_sha256="sha256:" + hashlib.sha256(tsr).hexdigest(),
        )
    manifest.update(manifest_extra or {})
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("attestations/run.intoto.json", b'{"payload":"x"}')
        if tsr is not None:
            zf.writestr("manifest.dsse.tsr", tsr)
    return path


# ---------------------------------------------------------------------------
# Trusted timestamp — reported when present, missing only when genuinely absent
# ---------------------------------------------------------------------------


def test_timestamp_present_is_complete(tmp_path: Path) -> None:
    _, run_id, ts = rc.read_bundle(_bundle(tmp_path, tsr=TSR_GRANTED))
    row = timestamp_row("b.zip", ts)
    assert run_id == "run-1"
    assert row.status == "complete" and row.reason is None
    assert row.facts["tsa_url"] == "https://tsa.example"
    assert row.facts["pki_status"] == "0"
    assert row.source_refs == ["b.zip!manifest.dsse.tsr"]


def test_timestamp_absent_is_missing_with_reason(tmp_path: Path) -> None:
    _, _, ts = rc.read_bundle(_bundle(tmp_path))
    row = timestamp_row("b.zip", ts)
    assert row.status == "missing" and "--timestamp not requested" in (row.reason or "")
    assert row.source_refs == [] and row.evidence_source is EvidenceSource.unverifiable


def test_timestamp_failed_request_reason(tmp_path: Path) -> None:
    extra = {"timestamp_status": "failed", "timestamp_failure_reason": "TSA unreachable"}
    _, _, ts = rc.read_bundle(_bundle(tmp_path, manifest_extra=extra))
    row = timestamp_row("b.zip", ts)
    assert row.status == "missing" and "TSA unreachable" in (row.reason or "")


def test_timestamp_status_ok_without_tsr(tmp_path: Path) -> None:
    _, _, ts = rc.read_bundle(_bundle(tmp_path, manifest_extra={"timestamp_status": "ok"}))
    assert "manifest.dsse.tsr is absent" in (timestamp_row("b", ts).reason or "")


@pytest.mark.parametrize(
    ("tsr", "extra", "needle"),
    [
        (TSR_GRANTED, {"manifest_dsse_tsr_sha256": "0" * 64}, "digest does not match"),
        (TSR_REJECTED, None, "PKIStatus=2"),
        (TSR_GARBAGE, None, "could not be parsed"),
    ],
)
def test_timestamp_defects_are_partial(
    tmp_path: Path, tsr: bytes, extra: dict[str, object] | None, needle: str
) -> None:
    _, _, ts = rc.read_bundle(_bundle(tmp_path, tsr=tsr, manifest_extra=extra))
    row = timestamp_row("b.zip", ts)
    assert row.status == "partial" and needle in (row.reason or "")


def test_reads_what_export_evidence_writes(tmp_path: Path, monkeypatch) -> None:
    """Round-trip through the shipped ADR-0030 bundle post-processor (TSA call stubbed)."""
    from novafabric.cli import export_evidence

    path = _bundle(tmp_path)
    monkeypatch.setattr(export_evidence, "add_rfc3161_timestamp", lambda _b, _u: TSR_GRANTED)
    export_evidence._add_timestamp_to_bundle(path, "https://tsa.example", False)
    _, _, ts = rc.read_bundle(path)
    assert timestamp_row(path.name, ts).status == "complete"


def test_reads_optional_failure_written_by_export_evidence(tmp_path: Path, monkeypatch) -> None:
    from novafabric.cli import export_evidence
    from novafabric.trust._rfc3161 import TimestampError

    def boom(_b: bytes, _u: str) -> bytes:
        raise TimestampError("tsa down")

    path = _bundle(tmp_path)
    monkeypatch.setattr(export_evidence, "add_rfc3161_timestamp", boom)
    export_evidence._add_timestamp_to_bundle(path, "https://tsa.example", True)
    _, _, ts = rc.read_bundle(path)
    row = timestamp_row(path.name, ts)
    assert row.status == "missing" and "tsa down" in (row.reason or "")


@pytest.mark.parametrize("content", [b"not a zip", None, "list", "norun"])
def test_corrupt_bundle_raises(tmp_path: Path, content: object) -> None:
    path = tmp_path / "bad.zip"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        with zipfile.ZipFile(path, "w") as zf:
            if content == "list":
                zf.writestr("manifest.json", "[]")
            elif content == "norun":
                zf.writestr("manifest.json", json.dumps({"subject": {}}))
            else:
                zf.writestr("other.txt", "x")
    with pytest.raises(rc.CorruptEvidenceError):
        rc.read_bundle(path)


# ---------------------------------------------------------------------------
# WORM
# ---------------------------------------------------------------------------


def _worm(**kw: object) -> WormFacts:
    base: dict[str, object] = {
        "ref": "r",
        "backend_type": "s3",
        "locked_until": NOW + timedelta(days=10),
        "confirmation": "etag",
    }
    base.update(kw)
    return WormFacts.model_validate(base)


def test_worm_rows() -> None:
    assert worm_row("r", None, as_of=NOW).status == "missing"
    ok = worm_row("r", _worm(), as_of=NOW)
    assert ok.status == "complete" and ok.facts["confirmation"] == "etag"
    local = worm_row("r", _worm(backend_type="local", confirmation=None), as_of=NOW)
    assert (local.status, local.reason) == ("partial", LOCAL_WORM_REASON)
    expired = worm_row("r", _worm(locked_until=NOW - timedelta(days=1)), as_of=NOW)
    assert expired.status == "partial" and "expired" in (expired.reason or "")
    bad = worm_row("r", _worm(integrity_ok=False), as_of=NOW)
    assert bad.status == "partial" and "integrity" in (bad.reason or "")


def test_read_local_worm(tmp_path: Path) -> None:
    db = tmp_path / "worm.db"
    LocalWormAdapter(db).put("run-1", b"capsule", retention_days=30)
    LocalWormAdapter(db).put("run-2", b"other", retention_days=30)
    got = rc.read_local_worm(db, ["run-1"])
    assert list(got) == ["run-1"]
    assert got["run-1"].backend_type == "local" and got["run-1"].integrity_ok is True
    assert rc.read_local_worm(tmp_path / "absent.db", ["run-1"]) == {}
    assert not (tmp_path / "absent.db").exists()


def test_read_local_worm_tampered_and_corrupt(tmp_path: Path) -> None:
    db = tmp_path / "worm.db"
    LocalWormAdapter(db).put("run-1", b"capsule", retention_days=30)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE worm_capsules SET data = ? WHERE capsule_id = 'run-1'", (b"x",))
    assert rc.read_local_worm(db, ["run-1"])["run-1"].integrity_ok is False
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"garbage" * 500)
    with pytest.raises(rc.CorruptEvidenceError):
        rc.read_local_worm(corrupt, ["run-1"])


def test_read_worm_receipts(tmp_path: Path) -> None:
    receipt = {
        "capsule_id": "run-1",
        "backend_type": "s3",
        "locked_until": "2030-01-01T00:00:00+00:00",
        "backend_confirmation_token": "etag-1",
    }
    single = tmp_path / "one.json"
    single.write_text(json.dumps(receipt))
    assert rc.read_worm_receipts(single)["run-1"].confirmation == "etag-1"
    many = tmp_path / "many.json"
    many.write_text(json.dumps([receipt, {**receipt, "capsule_id": "run-2"}]))
    assert set(rc.read_worm_receipts(many)) == {"run-1", "run-2"}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"capsule_id": "x"}))
    with pytest.raises(rc.CorruptEvidenceError):
        rc.read_worm_receipts(bad)


# ---------------------------------------------------------------------------
# Registry posture: retention policy + legal hold
# ---------------------------------------------------------------------------


def test_policy_and_hold_posture(tmp_path: Path) -> None:
    (tmp_path / "retention-policy.yaml").write_text(
        "registry: prod\nretention_days: 2190\ndeletion_mode: prohibited\n"
        "jurisdiction: US\nlegal_hold_ids: [h1]\n"
    )
    (tmp_path / "holds.jsonl").write_text(
        json.dumps({"hold_id": "h1", "reason": "sec", "released_at": None})
        + "\n\n"
        + json.dumps({"hold_id": "h0", "reason": "old", "released_at": "2026-01-01"})
        + "\n"
    )
    p = policy_row("prod", rc.read_policy(tmp_path))
    assert p.status == "complete" and p.facts["retention_days"] == "2190"
    assert p.facts["deletion_mode"] == "prohibited"
    h = hold_row("prod", rc.read_holds(tmp_path))
    assert h.status == "complete"
    assert h.facts["active_hold_ids"] == "h1" and h.facts["released_hold_ids"] == "h0"


def test_posture_absent(tmp_path: Path) -> None:
    assert rc.read_policy(tmp_path) is None
    assert policy_row("prod", None).status == "missing"
    assert policy_row(None, None).status == "missing"
    assert hold_row(None, None).status == "missing"
    h = hold_row("prod", rc.read_holds(tmp_path))
    assert h.status == "complete" and "absent" in h.facts["hold_record"]


def test_corrupt_policy_and_holds_raise(tmp_path: Path) -> None:
    (tmp_path / "retention-policy.yaml").write_text("registry: prod\n")  # no retention_days
    with pytest.raises(rc.CorruptEvidenceError):
        rc.read_policy(tmp_path)
    (tmp_path / "retention-policy.yaml").write_text("a: [unclosed\n")
    with pytest.raises(rc.CorruptEvidenceError):
        rc.read_policy(tmp_path)
    (tmp_path / "holds.jsonl").write_text("{not json}\n")
    with pytest.raises(rc.CorruptEvidenceError):
        rc.read_holds(tmp_path)


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def test_audit_rows(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    log = AuditLog(log_path)
    log.append(AuditEventType.RETENTION_ACTION, "cron", "run-1", {"a": 1})
    log.append(AuditEventType.CAPSULE_DELETE, "cli", "other-run")
    index = rc.AuditIndex(log_path)
    ok = audit_row("run-1", index.facts_for(["run-1"]), log_ref=None)
    assert ok.status == "complete" and ok.facts["event_types"] == "retention.action"
    none = audit_row("run-9", index.facts_for(["run-9"]), log_ref=None)
    assert none.status == "missing" and "run-9" in (none.reason or "")
    assert "at /x" in (audit_row("r", None, log_ref="/x").reason or "")


def test_audit_chain_break_and_parse_error(tmp_path: Path) -> None:
    log_path = tmp_path / "audit.jsonl"
    log = AuditLog(log_path)
    log.append(AuditEventType.RETENTION_ACTION, "cron", "run-1")
    lines = log_path.read_text().splitlines()
    tampered = json.loads(lines[0])
    tampered["actor"] = "mallory"
    log_path.write_text(json.dumps(tampered) + "\n")
    row = audit_row("run-1", rc.AuditIndex(log_path).facts_for(["run-1"]), log_ref=None)
    assert row.status == "partial" and "hash chain" in (row.reason or "")
    log_path.write_text("{}\n")
    row = audit_row("run-1", rc.AuditIndex(log_path).facts_for(["run-1"]), log_ref=None)
    assert row.status == "partial" and "could not be fully parsed" in (row.reason or "")


def test_audit_facts_parse_error_without_entries() -> None:
    facts = AuditFacts(ref="x", parse_error="bad")
    assert audit_row("r", facts, log_ref=None).status == "partial"


# ---------------------------------------------------------------------------
# End-to-end assembly
# ---------------------------------------------------------------------------


def test_collect_and_build(tmp_path: Path) -> None:
    b1 = _bundle(tmp_path, "b1.zip", run_id="run-1", tsr=TSR_GRANTED)
    b2 = _bundle(tmp_path, "b2.zip", run_id="run-2")
    worm_db = tmp_path / "worm.db"
    LocalWormAdapter(worm_db).put("run-1", b"c", retention_days=30)
    LocalWormAdapter(worm_db).put("run-2", b"c", retention_days=30)
    receipts = tmp_path / "r.json"
    receipts.write_text(
        json.dumps(
            {
                "capsule_id": "run-2",
                "backend_type": "s3",
                "locked_until": "2099-01-01T00:00:00+00:00",
                "backend_confirmation_token": "e",
            }
        )
    )
    log_path = tmp_path / "audit.jsonl"
    AuditLog(log_path).append(AuditEventType.RETENTION_ACTION, "cron", "BND1")
    facts = rc.collect_bundle_facts(
        [b1, b2], worm_db=worm_db, worm_receipts=receipts, audit_log=log_path
    )
    att = build_retention_attestation(
        regime=Regime.mifid,
        as_of=datetime.now(UTC),
        registry=None,
        policy=None,
        holds=None,
        bundles=facts,
    )
    assert att.regime == REGIME_TEXT[Regime.mifid]
    rows1 = {r.element: r for r in att.artifacts[0].rows}
    rows2 = {r.element: r for r in att.artifacts[1].rows}
    assert rows1["worm_lock"].status == "partial"  # local adapter only
    assert rows2["worm_lock"].status == "complete"  # cloud receipt wins
    assert rows1["trusted_timestamp"].status == "complete"
    assert rows2["trusted_timestamp"].status == "missing"
    assert rows1["audit_trail"].status == "complete"  # matched on bundle_id
    assert sum(att.summary.values()) == 2 + 3 * 2
    dumped = json.dumps(att.model_dump(mode="json"))
    for forbidden in ('"compliant"', '"verdict"', '"rating"'):
        assert forbidden not in dumped


def test_collect_without_audit_log(tmp_path: Path) -> None:
    facts = rc.collect_bundle_facts(
        [_bundle(tmp_path)], worm_db=None, worm_receipts=None, audit_log=tmp_path / "none.jsonl"
    )
    assert facts[0].audit is None and facts[0].worm is None
    assert not (tmp_path / "none.jsonl").exists()


def test_naive_as_of_rejected() -> None:
    with pytest.raises(ValueError):
        build_retention_attestation(
            regime=Regime.sec_17a_4,
            as_of=datetime(2026, 1, 1),
            registry=None,
            policy=None,
            holds=None,
            bundles=[],
        )


def test_bundle_facts_model_roundtrip() -> None:
    bf = BundleFacts(bundle="x", run_id="r", timestamp=TimestampFacts())
    assert bf.worm is None and bf.timestamp.tsr_present is False


def test_cli_regime_option_mirrors_model() -> None:
    from novafabric.cli.export_retention import RegimeOption

    assert {o.value for o in RegimeOption} == {r.value for r in Regime}


# ---------------------------------------------------------------------------
# Render-only: never write the WORM store; bounded bundle reads (reviewer regressions)
# ---------------------------------------------------------------------------


def _snapshot(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


def test_read_local_worm_leaves_db_untouched(tmp_path: Path) -> None:
    odd = tmp_path / "w?mode=rwc#x"
    odd.mkdir()
    db = odd / "worm.db"
    LocalWormAdapter(db).put("run-1", b"capsule", retention_days=30)
    before = _snapshot(db)
    got = rc.read_local_worm(db, ["run-1", "run-1", "absent"])
    assert list(got) == ["run-1"] and got["run-1"].integrity_ok is True
    assert _snapshot(db) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["w?mode=rwc#x"]


def test_read_local_worm_without_table_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / "worm.db"
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.close()
    before = _snapshot(db)
    assert rc.read_local_worm(db, ["run-1"]) == {}
    assert _snapshot(db) == before  # no worm_capsules DDL


def test_read_local_worm_missing_parent_not_created(tmp_path: Path) -> None:
    db = tmp_path / "nope" / "worm.db"
    assert rc.read_local_worm(db, ["run-1"]) == {}
    assert not db.parent.exists()


def test_oversize_manifest_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _bundle(tmp_path)
    monkeypatch.setattr(rc, "_MANIFEST_MAX_BYTES", 16)
    with pytest.raises(rc.CorruptEvidenceError, match="limit"):
        rc.read_bundle(path)


def test_oversize_tsr_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _bundle(tmp_path, tsr=TSR_GRANTED)
    monkeypatch.setattr(rc, "_TSR_MAX_BYTES", len(TSR_GRANTED) - 1)
    with pytest.raises(rc.CorruptEvidenceError, match="manifest.dsse.tsr"):
        rc.read_bundle(path)
    monkeypatch.setattr(rc, "_TSR_MAX_BYTES", len(TSR_GRANTED))
    assert rc.read_bundle(path)[2].tsr_present is True
