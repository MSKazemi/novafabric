"""ADR-0159 D2 / NF-276 — model-validation-independence renderer and its read-only collectors.

Acceptance criteria covered:
- validator != developer on an approved ADR-0058 record -> independence ``complete``;
- same identity (or same key) -> ``missing`` with reason "single-identity approval";
- no record / open proposal / bypass / failed SoD verification -> ``missing``, never fabricated;
- mixed records -> ``partial``; no verdict/rating/sufficiency field anywhere;
- collectors *read* the shipped stores (registry ``promotion_proposals`` read-only; seal bundles
  via the shipped ``verify_sod``) and never create a database.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from novafabric.compliance.export.finance import model_independence_collect as collect
from novafabric.compliance.export.finance.model_independence import (
    FINANCE_HONESTY_BANNER,
    INDEPENDENCE_REGIME,
    REASON_SINGLE_IDENTITY,
    MakerCheckerRecord,
    build_model_independence_file,
    classify_record,
)
from novafabric.compliance.export.provenance import EvidenceSource
from novafabric.promote.bundle_store import PromoteBundleStore
from novafabric.promote.policy_store import PolicyStore
from novafabric.promote.predicates import (
    APPROVAL_PAYLOAD_TYPE,
    BYPASS_PAYLOAD_TYPE,
    PROPOSAL_PAYLOAD_TYPE,
    build_approval_predicate,
    build_bypass_predicate,
    build_policy_predicate,
    build_proposal_predicate,
    sign_promote_envelope,
)
from novafabric.registry.store import get_connection, init_schema

KEYS = Path(__file__).parent / "fixtures" / "promote" / "keys"


def _rec(**kw: object) -> MakerCheckerRecord:
    base: dict[str, object] = {
        "source": "registry_promotion",
        "record_ref": "registry://promotion_proposals/p1",
        "subject": "scorer@1",
        "state": "approved",
        "maker": "alice",
        "checker": "bob",
        "maker_key_fp": "fp-a",
        "checker_key_fp": "fp-b",
    }
    base.update(kw)
    return MakerCheckerRecord.model_validate(base)


# ---------------------------------------------------------------------------
# Pure renderer
# ---------------------------------------------------------------------------


def test_distinct_identities_is_complete() -> None:
    f = build_model_independence_file(model_id="scorer", records=[_rec()])
    assert f.independence.status == "complete"
    assert f.independence.source_refs == ["registry://promotion_proposals/p1"]
    assert f.independence.reason is None
    assert f.independence.evidence_source is EvidenceSource.operator_asserted
    assert f.regime == INDEPENDENCE_REGIME
    assert f.banner == FINANCE_HONESTY_BANNER
    assert f.summary == {"complete": 1, "missing": 0}


def test_single_identity_is_missing_with_reason() -> None:
    f = build_model_independence_file(model_id="scorer", records=[_rec(checker="alice")])
    assert f.independence.status == "missing"
    assert f.independence.reason == REASON_SINGLE_IDENTITY
    assert f.independence.source_refs == []  # never fabricated
    assert f.independence.evidence_source is EvidenceSource.unverifiable


def test_same_key_fingerprint_is_single_identity() -> None:
    r = classify_record(_rec(checker_key_fp="fp-a"))
    assert (r.status, r.reason) == ("missing", REASON_SINGLE_IDENTITY)


def test_no_records_is_missing() -> None:
    f = build_model_independence_file(model_id="ghost", records=[])
    assert f.independence.status == "missing"
    assert "no ADR-0058 maker-checker record found for ghost" == f.independence.reason
    assert f.records == []


def test_open_proposal_bypass_and_unverified_are_missing() -> None:
    open_ = classify_record(_rec(state="open", checker=None, checker_key_fp=None))
    assert open_.status == "missing" and "state=open" in (open_.reason or "")
    bypass = classify_record(_rec(bypass_used=True))
    assert bypass.status == "missing" and "bypass" in (bypass.reason or "")
    bad = classify_record(_rec(verification_failure="proposal_digest mismatch"))
    assert bad.status == "missing" and "proposal_digest mismatch" in (bad.reason or "")


def test_mixed_records_are_partial() -> None:
    f = build_model_independence_file(
        model_id="scorer",
        records=[_rec(), _rec(record_ref="registry://promotion_proposals/p2", checker=None)],
    )
    assert f.independence.status == "partial"
    assert "1 of 2" in (f.independence.reason or "")
    assert f.summary == {"complete": 1, "missing": 1}


def test_all_missing_without_single_identity_reason() -> None:
    f = build_model_independence_file(model_id="scorer", records=[_rec(state="open", checker=None)])
    assert f.independence.status == "missing"
    assert f.independence.reason == "no independently counter-signed maker-checker record"


def test_no_verdict_field_anywhere() -> None:
    dumped = json.dumps(
        build_model_independence_file(model_id="m", records=[_rec()]).model_dump(mode="json")
    )
    for forbidden in ('"rating"', '"verdict"', '"compliant"', '"sufficient"', '"score"'):
        assert forbidden not in dumped


# ---------------------------------------------------------------------------
# Registry collector (read-only)
# ---------------------------------------------------------------------------


def _registry(tmp_path: Path, rows: list[tuple[object, ...]]) -> Path:
    db = tmp_path / "registry.db"
    conn = get_connection(db)
    init_schema(conn, force=True)
    conn.executemany(
        "INSERT INTO promotion_proposals (proposal_id, asset_name, asset_version, to_status, "
        "proposer, proposer_key_fp, proposer_sig, proposed_at, state, approver, "
        "approver_key_fp, approver_sig, approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


_ROW_OK = (
    "p1",
    "scorer",
    "1",
    "staging",
    "alice",
    "fp-a",
    "s",
    "2026-01-01T00:00:00",
    "approved",
    "bob",
    "fp-b",
    "s2",
    "2026-01-02T00:00:00",
)
_ROW_SELF = (
    "p2",
    "scorer",
    "2",
    "staging",
    "alice",
    "fp-a",
    "s",
    "2026-01-03T00:00:00",
    "approved",
    "alice",
    "fp-c",
    "s2",
    "2026-01-04T00:00:00",
)
_ROW_OTHER = (
    "p3",
    "other",
    "1",
    "staging",
    "carol",
    "fp-x",
    "s",
    "2026-01-05T00:00:00",
    "open",
    None,
    None,
    None,
    None,
)


def test_registry_collector_reads_records(tmp_path: Path) -> None:
    db = _registry(tmp_path, [_ROW_OK, _ROW_SELF, _ROW_OTHER])
    recs = collect.collect_registry_records(db, "scorer")
    assert [r.record_ref for r in recs] == [
        "registry://promotion_proposals/p1",
        "registry://promotion_proposals/p2",
    ]
    assert recs[0].maker == "alice" and recs[0].checker == "bob"
    assert recs[0].subject == "scorer@1"
    f = build_model_independence_file(model_id="scorer", records=recs)
    assert f.independence.status == "partial"


def test_registry_collector_version_filter(tmp_path: Path) -> None:
    db = _registry(tmp_path, [_ROW_OK, _ROW_SELF])
    recs = collect.collect_registry_records(db, "scorer@2")
    assert len(recs) == 1
    f = build_model_independence_file(model_id="scorer@2", records=recs)
    assert (f.independence.status, f.independence.reason) == ("missing", REASON_SINGLE_IDENTITY)


def test_registry_collector_missing_db_creates_nothing(tmp_path: Path) -> None:
    db = tmp_path / "nope" / "registry.db"
    assert collect.collect_registry_records(db, "scorer") == []
    assert not db.exists() and not db.parent.exists()


def test_registry_collector_db_without_table(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    sqlite3.connect(db).execute("CREATE TABLE t (x)").connection.close()
    assert collect.collect_registry_records(db, "scorer") == []


def test_registry_collector_corrupt_db_raises(tmp_path: Path) -> None:
    db = tmp_path / "corrupt.db"
    db.write_bytes(b"this is not a sqlite database at all" * 100)
    with pytest.raises(collect.MakerCheckerSourceError):
        collect.collect_registry_records(db, "scorer")


@pytest.mark.parametrize("bad", ["", "@1", "scorer@"])
def test_split_model_id_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        collect.split_model_id(bad)


def test_split_model_id() -> None:
    assert collect.split_model_id("a") == ("a", None)
    assert collect.split_model_id("org@a@2") == ("org@a", "2")


# ---------------------------------------------------------------------------
# Seal (DSSE) collector — outcome from the shipped verify_sod
# ---------------------------------------------------------------------------

CAPSULE = "cap-nf276"


def _sign(pred: dict[str, object], ptype: str, who: str) -> bytes:
    return sign_promote_envelope(
        json.dumps(pred).encode(), ptype, KEYS / f"{who}.pem", KEYS / f"{who}_cert.pem"
    )


def _seal(
    tmp_path: Path,
    *,
    approver: str | None = "approver",
    policy: bool = True,
    approvers: tuple[str, ...] = ("approver", "proposer"),
) -> tuple[Path, Path]:
    policy_db = tmp_path / "merkle.db"
    version = "1"
    if policy:
        store = PolicyStore(policy_db)
        version = str(store.put(json.dumps(build_policy_predicate(["proposer"], list(approvers)))))
        store.close()
    bundles = PromoteBundleStore(tmp_path)
    proposal = _sign(
        build_proposal_predicate(
            CAPSULE,
            "a" * 64,
            "staging",
            "A justification long enough for the schema.",
            "proposer",
            version,
        ),
        PROPOSAL_PAYLOAD_TYPE,
        "proposer",
    )
    uuid = bundles.put_proposal(CAPSULE, proposal)
    if approver is not None:
        time.sleep(0.01)
        approval = _sign(
            build_approval_predicate(proposal, "approved", approver),
            APPROVAL_PAYLOAD_TYPE,
            approver,
        )
        bundles.put_approval(CAPSULE, approval, uuid)
    return tmp_path, policy_db


def test_seal_independent_is_complete(tmp_path: Path) -> None:
    data_dir, policy_db = _seal(tmp_path)
    rec = collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
    assert rec is not None
    assert (rec.state, rec.maker, rec.checker) == ("verified", "proposer", "approver")
    assert rec.proposed_at and rec.approved_at
    assert classify_record(rec).status == "complete"


def test_seal_self_approval_is_single_identity(tmp_path: Path) -> None:
    data_dir, policy_db = _seal(tmp_path, approver="proposer")
    rec = collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
    assert rec is not None and rec.state == "self-approved"
    assert classify_record(rec).reason == REASON_SINGLE_IDENTITY


def test_seal_open_proposal(tmp_path: Path) -> None:
    data_dir, policy_db = _seal(tmp_path, approver=None)
    rec = collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
    assert rec is not None and rec.state == "open" and rec.checker is None
    assert classify_record(rec).status == "missing"


def test_seal_missing_policy_db_is_unverified_and_not_created(tmp_path: Path) -> None:
    data_dir, policy_db = _seal(tmp_path, policy=False)
    rec = collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
    assert not policy_db.exists()
    assert rec is not None and rec.state == "unverified"
    assert "policy not found" in (rec.verification_failure or "")
    assert classify_record(rec).status == "missing"


def test_seal_no_bundle_is_none(tmp_path: Path) -> None:
    assert (
        collect.collect_seal_record("nothing", data_dir=tmp_path, policy_db=tmp_path / "m.db")
        is None
    )


def test_seal_bypass_is_missing(tmp_path: Path) -> None:
    bundles = PromoteBundleStore(tmp_path)
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    env = _sign(
        build_bypass_predicate(
            CAPSULE, "a" * 64, "production", "incident hotfix", "admin", until, []
        ),
        BYPASS_PAYLOAD_TYPE,
        "admin",
    )
    bundles.put_bypass(CAPSULE, env, until)
    rec = collect.collect_seal_record(CAPSULE, data_dir=tmp_path, policy_db=tmp_path / "m.db")
    assert rec is not None and rec.bypass_used and rec.state == "bypassed"
    assert rec.record_ref.endswith("/bypass")
    assert "bypass" in (classify_record(rec).reason or "")


def test_envelope_subject_unverifiable_yields_none() -> None:
    assert collect._envelope_subject(b"{}", PROPOSAL_PAYLOAD_TYPE) == (None, None)


# ---------------------------------------------------------------------------
# Render-only: the collectors never write an evidence store (reviewer regressions)
# ---------------------------------------------------------------------------


def _snapshot(path: Path) -> tuple[bytes, int]:
    return path.read_bytes(), path.stat().st_mtime_ns


def test_registry_collector_path_with_uri_metacharacters(tmp_path: Path) -> None:
    """A ``?``/``#`` in the path must not truncate the URI, drop ``mode=ro`` or create a DB."""
    odd = tmp_path / "reg?mode=rwc#frag"
    odd.mkdir()
    db = _registry(odd, [_ROW_OK])
    before = _snapshot(db)
    recs = collect.collect_registry_records(db, "scorer")
    assert [r.record_ref for r in recs] == ["registry://promotion_proposals/p1"]
    assert _snapshot(db) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["reg?mode=rwc#frag"]


def test_registry_collector_missing_db_under_uri_metacharacters(tmp_path: Path) -> None:
    db = tmp_path / "x?y#z" / "registry.db"
    assert collect.collect_registry_records(db, "scorer") == []
    assert list(tmp_path.iterdir()) == []


def _plain_policy_db(path: Path, bundle_json: str) -> None:
    """A rollback-journal policy DB, so a WAL flip by the reader would show in the header."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE promote_policy (id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT "
        "NOT NULL DEFAULT 'default', version INTEGER NOT NULL, bundle_json TEXT NOT NULL, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO promote_policy (namespace, version, bundle_json, created_at) "
        "VALUES ('default', 1, ?, '2026-01-01T00:00:00+00:00')",
        (bundle_json,),
    )
    conn.commit()
    conn.close()


def test_seal_collector_leaves_policy_db_untouched(tmp_path: Path) -> None:
    data_dir, _ = _seal(tmp_path, policy=False)  # proposal references policy version "1"
    policy_db = tmp_path / "plain.db"
    _plain_policy_db(policy_db, json.dumps(build_policy_predicate(["proposer"], ["approver"])))
    before = _snapshot(policy_db)
    assert before[0][18:20] == b"\x01\x01"  # rollback journal, not WAL
    rec = collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
    assert rec is not None and rec.state == "verified"
    assert _snapshot(policy_db) == before  # no WAL flip, no DDL, same mtime
    assert not policy_db.with_name("plain.db-wal").exists()


def test_seal_collector_policy_db_without_table_is_unverified(tmp_path: Path) -> None:
    data_dir, _ = _seal(tmp_path, policy=False)
    policy_db = tmp_path / "empty.db"
    sqlite3.connect(policy_db).execute("CREATE TABLE t (x)").connection.close()
    before = _snapshot(policy_db)
    rec = collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
    assert rec is not None and rec.state == "unverified"
    assert _snapshot(policy_db) == before  # promote_policy table was not created


def test_seal_collector_missing_policy_db_parent_not_created(tmp_path: Path) -> None:
    data_dir, _ = _seal(tmp_path, policy=False)
    policy_db = tmp_path / "no?such#dir" / "merkle.db"
    rec = collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
    assert rec is not None and rec.state == "unverified"
    assert not policy_db.parent.exists()


def test_seal_collector_policy_db_uri_metacharacters(tmp_path: Path) -> None:
    odd = tmp_path / "pol?mode=rwc#x"
    odd.mkdir()
    data_dir, _ = _seal(tmp_path, policy=False)
    policy_db = odd / "plain.db"
    _plain_policy_db(policy_db, json.dumps(build_policy_predicate(["proposer"], ["approver"])))
    before = _snapshot(policy_db)
    rec = collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
    assert rec is not None and rec.state == "verified"
    assert _snapshot(policy_db) == before


def test_seal_collector_corrupt_policy_db_raises(tmp_path: Path) -> None:
    data_dir, _ = _seal(tmp_path, policy=False)
    policy_db = tmp_path / "corrupt.db"
    policy_db.write_bytes(b"not sqlite" * 200)
    with pytest.raises(collect.MakerCheckerSourceError):
        collect.collect_seal_record(CAPSULE, data_dir=data_dir, policy_db=policy_db)
