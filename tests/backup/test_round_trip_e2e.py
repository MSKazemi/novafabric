"""ADR-0216 acceptance: the full round-trip — back up ALL the things, restore
into a fresh home, and prove NovaFabric can OPEN AND READ every one of them.

Home A is populated through the real subsystem APIs (registry, lineage, PII
DEK store + AES-GCM ciphertext, seal Merkle log, ratchet, incidents, metadata
store, spool, audit log with an applied crypto-shred). The set is created,
verified offline, restored into home B, and every store is then read back
through the same real APIs — including PII decryption with the restored DEK
and the D4 guarantee that the shredded subject stays gone.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from typer.testing import CliRunner

from novafabric.backup.create import create_backup
from novafabric.backup.restore import restore_backup
from novafabric.backup.verify import verify_backup
from novafabric.cli.main import app
from novafabric.compliance.incident.models import Incident, IncidentSeverity
from novafabric.compliance.incident.store import IncidentStore
from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import LineageEdge, LineageNode, node_id_for
from novafabric.metadata_store.sqlite import SQLiteMetadataStore
from novafabric.pii.dek.store import DEKStore
from novafabric.registry.service import list_assets, register_asset
from novafabric.spec.models import ModelSpec
from novafabric.trust.novaseal.merkle import open_merkle_log
from novafabric.trust.novaseal.ratchet import init_ratchet, load_state, rotate

runner = CliRunner()

RUN_ID = "run-e2e-001"
PLAINTEXT = b"subject-1 personal data"


@pytest.fixture()
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


def _populate_home(home: Path, audit_log: Path, receipt_dir: Path) -> dict[str, object]:
    """Fill home A via the real subsystem APIs; return facts to re-check in B."""
    facts: dict[str, object] = {}
    home.mkdir(parents=True)

    # 1. Registry: a real registered asset.
    spec = ModelSpec(
        novafabric_spec_version="0.1.0",
        name="e2e-model",
        version="1.0.0",
        spec={"framework": "pytorch", "artifact_path": "model.pt"},
    )
    register_asset(spec, home / "e2e-model.yaml", db_path=home / "registry.db")

    # 2. Lineage: run -> artifact edge in the shared registry DB.
    run_node = LineageNode(
        node_id=node_id_for("run", RUN_ID),
        kind="run",
        ref=RUN_ID,
        first_seen_capsule_run_id=RUN_ID,
        payload={},
    )
    artifact_ref = f"artifact:{RUN_ID}:out.txt"
    artifact_node = LineageNode(
        node_id=node_id_for("artifact", artifact_ref),
        kind="artifact",
        ref=artifact_ref,
        first_seen_capsule_run_id=RUN_ID,
        payload={},
    )
    edge = LineageEdge(
        edge_type="PRODUCED",
        source={"kind": "run", "run_id": RUN_ID},
        target={
            "kind": "artifact",
            "artifact_ref": {"capsule_run_id": RUN_ID, "path": "out.txt"},
        },
        confidence="high",
        capsule_run_id=RUN_ID,
    )
    store = LineageStore(db_path=home / "registry.db")
    store.replace_capsule_lineage([run_node, artifact_node], [edge], RUN_ID)
    store._conn.close()
    facts["artifact_ref"] = artifact_ref

    # 3. A capsule with evidence streams.
    capsule = home / "capsules" / RUN_ID
    capsule.mkdir(parents=True)
    (capsule / "trace.jsonl").write_text('{"event": "start"}\n{"event": "end"}\n')
    (capsule / "manifest.json").write_text(json.dumps({"run_id": RUN_ID}))
    facts["trace_sha"] = hashlib.sha256((capsule / "trace.jsonl").read_bytes()).hexdigest()

    # 4. PII DEK store: subject-1 alive (with real ciphertext), subject-2 will
    #    be shredded per the audit log but its DEK is still present — the
    #    resurrection scenario D4 replay must fix.
    deks = DEKStore(home / "dek.db")
    dek1 = deks.get_or_create_dek("subject-1")
    deks.get_or_create_dek("subject-2")
    nonce = b"\x00" * 12
    facts["ciphertext"] = AESGCM(bytes.fromhex(dek1.dek_hex)).encrypt(
        nonce, PLAINTEXT, None
    )

    # 5. Seal transparency log with real leaves.
    log = open_merkle_log(home / "novaseal-merkle.db")
    log.append({"seal": 1})
    log.append({"seal": 2})

    # 6. Forward-secure ratchet, one rotation.
    init_ratchet("node-e2e", home / "seal" / "ratchet")
    rotate("node-e2e", home / "seal" / "ratchet")

    # 7. Incident.
    incident = Incident(
        title="e2e incident",
        classification="unauthorized_tool_use",
        severity=IncidentSeverity.LOW,
        run_ids=[RUN_ID],
        occurred_at=datetime.now(timezone.utc),
    )
    with IncidentStore(home / "incidents.db") as incidents:
        incidents.create(incident)
    facts["incident_id"] = incident.id

    # 8. Metadata store run row.
    run_uuid, tenant_uuid = uuid.uuid4(), uuid.uuid4()
    metadata = SQLiteMetadataStore(home / "metadata.db")
    metadata.bootstrap()
    metadata.register_run(run_uuid, tenant_uuid)
    facts["run_uuid"], facts["tenant_uuid"] = run_uuid, tenant_uuid

    # 9. Spool + dashboard audit.
    (home / "spool").mkdir()
    (home / "spool" / "00000001.jsonl").write_text('{"spooled": true}\n')
    (home / "dashboard-audit.jsonl").write_text('{"mutation": "none"}\n')

    # 10. Audit log with an APPLIED crypto-shred for subject-2.
    receipt = receipt_dir / "receipt-s2.json"
    receipt.write_text(json.dumps({"subject_id": "subject-2"}))
    audit_log.parent.mkdir(parents=True, exist_ok=True)
    audit_log.write_text(
        json.dumps(
            {
                "event_type": "retention.action",
                "details": {
                    "action": "crypto-shred",
                    "outcome": "applied",
                    "erasure_receipt_ref": str(receipt),
                },
            }
        )
        + "\n"
    )
    return facts


@pytest.mark.usefixtures("_no_signing")
def test_backup_everything_restore_open_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_a = tmp_path / "home-a"
    audit_a = tmp_path / "audit-a" / "audit.jsonl"
    facts = _populate_home(home_a, audit_a, tmp_path)

    # --- create + offline verify -------------------------------------------
    result = create_backup(tmp_path / "set.tar.gz", home=home_a, audit_log_path=audit_a)
    included = {c.component for c in result.manifest.coverage if c.status == "included"}
    assert {
        "registry", "capsules", "incidents", "metadata", "dek", "tsa-nonces",
        "seal-merkle", "ratchet", "dashboard-audit", "spool", "audit-log",
    } <= included | {"tsa-nonces"}  # tsa_nonces.db only exists after TSA use
    assert verify_backup(result.archive_path).ok is True

    # CLI verify agrees, exit 0.
    cli = runner.invoke(app, ["backup", "verify", str(result.archive_path)])
    assert cli.exit_code == 0, cli.output

    # --- restore into a fresh home B ---------------------------------------
    home_b = tmp_path / "home-b"
    audit_b = tmp_path / "audit-b" / "audit.jsonl"
    out = restore_backup(result.archive_path, home=home_b, audit_log_path=audit_b)
    assert out.ok is True, [s for s in out.steps if not s.ok]

    # --- OPEN AND READ everything in B through the real APIs ----------------
    # Registry: the asset is queryable.
    assets = list_assets(None, None, db_path=home_b / "registry.db")
    assert [a["name"] for a in assets] == ["e2e-model"]

    # Lineage: provenance of the run reaches the produced artifact.
    store = LineageStore(db_path=home_b / "registry.db")
    provenance = store.provenance(RUN_ID, "run")
    store._conn.close()
    assert facts["artifact_ref"] in {n["ref"] for n in provenance}

    # Capsule: evidence stream byte-identical and readable.
    trace = home_b / "capsules" / RUN_ID / "trace.jsonl"
    assert hashlib.sha256(trace.read_bytes()).hexdigest() == facts["trace_sha"]
    assert [json.loads(line)["event"] for line in trace.read_text().splitlines()] == [
        "start",
        "end",
    ]

    # PII: subject-1 decrypts with the RESTORED DEK; subject-2 stays shredded.
    deks_b = DEKStore(home_b / "dek.db")
    dek1 = deks_b.get_dek("subject-1")
    assert dek1 is not None
    plain = AESGCM(bytes.fromhex(dek1.dek_hex)).decrypt(
        b"\x00" * 12, facts["ciphertext"], None
    )
    assert plain == PLAINTEXT
    assert deks_b.get_dek("subject-2") is None  # D4: shredded stays shredded

    # Seal log: consistent with both leaves.
    verdict = open_merkle_log(home_b / "novaseal-merkle.db").verify_consistency()
    assert verdict.consistent and verdict.leaf_count == 2
    seal_step = next(s for s in out.steps if s.name == "verify-seal-log")
    assert "2 leaves" in seal_step.detail

    # Ratchet: node state readable at its rotated epoch.
    assert load_state("node-e2e", home_b / "seal" / "ratchet").epoch == 1

    # Incidents + metadata rows readable.
    with IncidentStore(home_b / "incidents.db") as incidents_b:
        assert [i.id for i in incidents_b.list_all()] == [facts["incident_id"]]
    metadata_b = SQLiteMetadataStore(home_b / "metadata.db")
    assert (
        metadata_b.lookup_run(facts["run_uuid"], facts["tenant_uuid"]) is not None  # type: ignore[arg-type]
    )

    # Spool + dashboard audit + audit log restored to their roots.
    assert (home_b / "spool" / "00000001.jsonl").read_text() == '{"spooled": true}\n'
    assert (home_b / "dashboard-audit.jsonl").is_file()
    assert audit_b.read_text() == audit_a.read_text()

    # Doctor agrees the restored home is healthy (CLI, exit 0).
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home_b))
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(home_b / "registry.db"))
    cli = runner.invoke(app, ["doctor", "--check-storage"])
    assert cli.exit_code == 0, cli.output
