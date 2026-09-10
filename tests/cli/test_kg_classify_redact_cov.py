"""Coverage-focused CLI tests for nova kg / classify / redact.

Targets previously-uncovered error, edge, and success branches in:
  - novafabric.cli.kg        (status/ingest/query/audit + alias/entity-queue)
  - novafabric.cli.classify  (run/list-vocabularies/from-capsule)
  - novafabric.cli.redact    (redact / subject-proof)

All tests are deterministic and use real temp files + SQLite. The only mocked
parts are the optional ``kuzu`` import (to exercise the ImportError branches)
and NovaSeal config presence — no network or external services are touched.
"""
from __future__ import annotations

import json
import sys
import unittest.mock as mock
from pathlib import Path

import pytest
import typer
import yaml
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli.classify import app as classify_app
from novafabric.cli.kg import kg_app
from novafabric.cli.redact import redact_cmd, subject_proof_cmd

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _redact_app() -> typer.Typer:
    """Wrap the plain-function redact commands in a Typer app for invocation."""
    app = typer.Typer()
    app.command("redact")(redact_cmd)
    app.command("subject-proof")(subject_proof_cmd)
    return app


def _extract_json_obj(output: str) -> dict:
    """Extract the trailing JSON object from console output that may be
    prefixed with rich warning lines (rich wraps text, so we slice from the
    first ``{`` to the last ``}``)."""
    start = output.index("{")
    end = output.rindex("}")
    return json.loads(output[start : end + 1])


def _gen_ed25519_key(path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.write_bytes(pem)


def _make_capsule(cap_dir: Path, run_id: str = "run-xyz", with_pii: bool = True) -> Path:
    cap_dir.mkdir(parents=True, exist_ok=True)
    (cap_dir / "capsule.yaml").write_text(yaml.safe_dump({"run_id": run_id}))
    body = (
        "my email is alice@example.com and key sk-ABCDEF1234567890abcdef"
        if with_pii
        else "no secrets here at all"
    )
    (cap_dir / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "a",
                "model_id": "m",
                "input": body,
            }
        )
        + "\n"
    )
    return cap_dir


# ===========================================================================
# nova kg — ImportError branches (kuzu unavailable)
# ===========================================================================


def _kuzu_absent() -> mock._patch_dict:
    """Context manager that makes ``import kuzu`` and kg.store fail."""
    return mock.patch.dict(sys.modules, {"kuzu": None, "novafabric.kg.store": None})


@pytest.mark.parametrize(
    "args",
    [
        ["init", "--path", "x.kuzu"],
        ["status", "--path", "x.kuzu"],
        ["query", "agent-1", "--path", "x.kuzu"],
        ["audit", "--path", "x.kuzu"],
    ],
)
def test_kg_commands_exit_1_without_kuzu(args: list[str]) -> None:
    with _kuzu_absent():
        result = runner.invoke(kg_app, args)
    assert result.exit_code == 1
    assert "Error" in result.output
    # NOTE: no sys.modules.pop here — mock.patch.dict restores the snapshot on
    # exit, so popping afterwards would evict the ORIGINAL kg.store module; the
    # next import then re-registers its prometheus collectors, the duplicate is
    # swallowed to None, and unrelated metrics tests in the same worker fail.


def test_kg_ingest_local_dir_exit_1_without_kuzu(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path / "cap")
    with _kuzu_absent():
        result = runner.invoke(kg_app, ["ingest", str(cap), "--path", "x.kuzu"])
    assert result.exit_code == 1


def test_kg_ingest_all_exit_1_without_kuzu(tmp_path: Path) -> None:
    caps = tmp_path / "capsules"
    caps.mkdir()
    with _kuzu_absent():
        result = runner.invoke(
            kg_app, ["ingest", "--all", "--capsule-dir", str(caps), "--path", "x.kuzu"]
        )
    assert result.exit_code == 1


# ===========================================================================
# nova kg status / query / audit — real KuzuDB store
# ===========================================================================


def test_kg_status_initialised(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "s.kuzu")
    assert runner.invoke(kg_app, ["init", "--path", kg]).exit_code == 0
    result = runner.invoke(kg_app, ["status", "--path", kg])
    assert result.exit_code == 0, result.output
    assert "health" in result.output.lower()
    assert "edge_count" in result.output


def test_kg_status_with_node_counts(tmp_path: Path) -> None:
    """Status after ingest renders the per-layer node-count table (144-151)."""
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "sn.kuzu")
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "a1",
                "model_id": "gpt-4o",
                "input_tokens": 1,
                "output_tokens": 1,
            }
        )
        + "\n"
    )
    assert runner.invoke(kg_app, ["ingest", str(cap), "--path", kg]).exit_code == 0
    result = runner.invoke(kg_app, ["status", "--path", kg])
    assert result.exit_code == 0, result.output
    assert "Node counts" in result.output


def test_kg_query_json_empty(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "q.kuzu")
    runner.invoke(kg_app, ["init", "--path", kg])
    result = runner.invoke(kg_app, ["query", "no-such-agent", "--path", kg])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["agent_id"] == "no-such-agent"
    assert data["models"] == []


def test_kg_query_text_with_data(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "qt.kuzu")
    cap = tmp_path / "cap"
    cap.mkdir()
    # The pipeline normalises agent ids to lowercase; use a lowercase id so the
    # query path matches and the text-rendering rows branch is exercised.
    (cap / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "agenttext",
                "model_id": "gpt-4o",
                "input_tokens": 10,
                "output_tokens": 5,
            }
        )
        + "\n"
    )
    ing = runner.invoke(kg_app, ["ingest", str(cap), "--path", kg])
    assert ing.exit_code == 0, ing.output
    result = runner.invoke(
        kg_app, ["query", "agenttext", "--path", kg, "--output", "text"]
    )
    assert result.exit_code == 0, result.output
    assert "Models called by agenttext" in result.output


def test_kg_query_text_no_entries(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "qne.kuzu")
    runner.invoke(kg_app, ["init", "--path", kg])
    result = runner.invoke(
        kg_app, ["query", "ghost", "--path", kg, "--output", "text"]
    )
    assert result.exit_code == 0, result.output
    assert "No KG entries" in result.output


def test_kg_query_error_branch(tmp_path: Path) -> None:
    """A bad store path triggers the generic exception handler (exit 1)."""
    pytest.importorskip("kuzu")
    # Point at an existing *file* so KuzuDB cannot open it as a DB directory.
    bad = tmp_path / "not-a-db"
    bad.write_text("garbage")
    result = runner.invoke(kg_app, ["query", "a", "--path", str(bad)])
    assert result.exit_code == 1
    assert "Error" in result.output


def test_kg_audit_text_and_json(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "a.kuzu")
    runner.invoke(kg_app, ["init", "--path", kg])
    text = runner.invoke(kg_app, ["audit", "--path", kg])
    assert text.exit_code == 0, text.output
    js = runner.invoke(kg_app, ["audit", "--path", kg, "--output", "json"])
    assert js.exit_code == 0
    assert "store_health" in json.loads(js.output)


# ===========================================================================
# nova kg ingest — local-dir success / edge paths
# ===========================================================================


def test_kg_ingest_local_dir_success(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "i.kuzu")
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "a1",
                "model_id": "gpt-4o",
                "input_tokens": 1,
                "output_tokens": 1,
            }
        )
        + "\n"
        + "not-json-line\n"  # exercises the JSONDecodeError skip branch
        + "\n"  # blank line skip branch
    )
    result = runner.invoke(kg_app, ["ingest", str(cap), "--path", kg])
    assert result.exit_code == 0, result.output
    assert "Ingested" in result.output
    assert "skipped" in result.output


def test_kg_ingest_events_jsonl_fallback(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "ev.kuzu")
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "events.jsonl").write_text(
        json.dumps(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "a2",
                "model_id": "m",
                "input_tokens": 1,
                "output_tokens": 1,
            }
        )
        + "\n"
    )
    result = runner.invoke(kg_app, ["ingest", str(cap), "--path", kg])
    assert result.exit_code == 0, result.output


def test_kg_ingest_no_event_files_exit_1(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "ne.kuzu")
    cap = tmp_path / "empty-cap"
    cap.mkdir()
    result = runner.invoke(kg_app, ["ingest", str(cap), "--path", kg])
    assert result.exit_code == 1
    assert "No events file" in result.output


def test_kg_ingest_not_a_directory_exit_1(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "nd.kuzu")
    afile = tmp_path / "afile"
    afile.write_text("x")
    result = runner.invoke(kg_app, ["ingest", str(afile), "--path", kg])
    assert result.exit_code == 1
    assert "not a directory" in result.output.lower()


def test_kg_ingest_missing_capsule_dir_arg_exit_1() -> None:
    result = runner.invoke(kg_app, ["ingest"])
    assert result.exit_code == 1
    assert "CAPSULE_DIR" in result.output


def test_kg_ingest_nats_import_error(tmp_path: Path) -> None:
    """--source nats with the consumer module absent hits its ImportError branch."""
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "nats.kuzu")
    with mock.patch.dict(sys.modules, {"novafabric.kg.consumer": None}):
        result = runner.invoke(kg_app, ["ingest", "--source", "nats", "--path", kg])
    assert result.exit_code == 1
    assert "Error" in result.output


# ===========================================================================
# nova kg ingest --all — bulk loop body
# ===========================================================================


def test_kg_ingest_all_success(tmp_path: Path) -> None:
    pytest.importorskip("kuzu")
    kg = str(tmp_path / "all.kuzu")
    caps = tmp_path / "capsules"
    c1 = caps / "run-001"
    c1.mkdir(parents=True)
    (c1 / "model-calls.jsonl").write_text(
        json.dumps(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "a1",
                "model_id": "gpt-4o",
                "input_tokens": 1,
                "output_tokens": 1,
            }
        )
        + "\n"
        + "garbage-not-json\n"  # exercises JSONDecodeError skip in --all loop
    )
    c2 = caps / "run-002"
    c2.mkdir(parents=True)
    (c2 / "events.jsonl").write_text(
        json.dumps(
            {
                "event_type": "ModelCallCompleted",
                "agent_id": "a2",
                "model_id": "claude",
                "input_tokens": 1,
                "output_tokens": 1,
            }
        )
        + "\n"
    )
    result = runner.invoke(
        kg_app, ["ingest", "--all", "--path", kg, "--capsule-dir", str(caps)]
    )
    assert result.exit_code == 0, result.output
    assert "Bulk ingest complete" in result.output


def test_kg_ingest_all_missing_base_dir_exit_1(tmp_path: Path) -> None:
    result = runner.invoke(
        kg_app, ["ingest", "--all", "--capsule-dir", str(tmp_path / "nope")]
    )
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


# ===========================================================================
# nova kg alias / entity-queue — populated render paths
# ===========================================================================


def test_kg_alias_list_empty(tmp_path: Path) -> None:
    db = str(tmp_path / "empty_alias.db")
    result = runner.invoke(kg_app, ["alias", "list", "nobody", "--alias-db", db])
    assert result.exit_code == 0, result.output
    assert "No aliases" in result.output


def test_kg_alias_list_json(tmp_path: Path) -> None:
    db = str(tmp_path / "alias.db")
    runner.invoke(
        kg_app,
        ["alias", "register", "alias-x", "canon-x", "--type", "tool", "--alias-db", db],
    )
    result = runner.invoke(
        kg_app, ["alias", "list", "canon-x", "--alias-db", db, "--output", "json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert any(e["alias"] == "alias-x" for e in data)


def test_kg_queue_list_empty(tmp_path: Path) -> None:
    db = str(tmp_path / "q.db")
    result = runner.invoke(kg_app, ["entity-queue", "list", "--queue-db", db])
    assert result.exit_code == 0, result.output
    assert "No pending" in result.output


def test_kg_queue_stats_text(tmp_path: Path) -> None:
    db = str(tmp_path / "q.db")
    result = runner.invoke(kg_app, ["entity-queue", "stats", "--queue-db", db])
    assert result.exit_code == 0, result.output
    assert "pending" in result.output.lower()


def test_kg_queue_approve_and_reject(tmp_path: Path) -> None:
    from novafabric.kg.review_queue import HumanReviewQueueWriter, ReviewItem

    qdb = tmp_path / "q.db"
    q = HumanReviewQueueWriter(db_path=qdb)
    a = ReviewItem(alias="to-approve", entity_type="model")
    r = ReviewItem(alias="to-reject", entity_type="model")
    q.enqueue(a)
    q.enqueue(r)
    q.close()

    appr = runner.invoke(
        kg_app,
        [
            "entity-queue",
            "approve",
            a.item_id,
            "--canonical",
            "gpt-4o",
            "--by",
            "tester",
            "--queue-db",
            str(qdb),
        ],
    )
    assert appr.exit_code == 0, appr.output
    assert "Approved" in appr.output

    rej = runner.invoke(
        kg_app,
        ["entity-queue", "reject", r.item_id, "--queue-db", str(qdb)],
    )
    assert rej.exit_code == 0, rej.output
    assert "Rejected" in rej.output


def test_kg_alias_register_then_list_table(tmp_path: Path) -> None:
    db = str(tmp_path / "alias.db")
    reg = runner.invoke(
        kg_app,
        ["alias", "register", "gpt-4o-2024", "gpt-4o", "--type", "model", "--alias-db", db],
    )
    assert reg.exit_code == 0, reg.output
    lst = runner.invoke(kg_app, ["alias", "list", "gpt-4o", "--alias-db", db])
    assert lst.exit_code == 0, lst.output
    assert "gpt-4o-2024" in lst.output


def test_kg_queue_list_and_stats_populated(tmp_path: Path) -> None:
    from novafabric.kg.review_queue import HumanReviewQueueWriter, ReviewItem

    qdb = tmp_path / "q.db"
    q = HumanReviewQueueWriter(db_path=qdb)
    q.enqueue(ReviewItem(alias="pending-model", entity_type="model"))
    q.close()

    lst = runner.invoke(kg_app, ["entity-queue", "list", "--queue-db", str(qdb)])
    assert lst.exit_code == 0, lst.output
    assert "pending" in lst.output.lower()

    js = runner.invoke(
        kg_app, ["entity-queue", "list", "--queue-db", str(qdb), "--output", "json"]
    )
    assert js.exit_code == 0
    assert any(i["alias"] == "pending-model" for i in json.loads(js.output))

    stats = runner.invoke(
        kg_app, ["entity-queue", "stats", "--queue-db", str(qdb), "--output", "json"]
    )
    assert stats.exit_code == 0
    assert json.loads(stats.output)["pending"] == 1


# ===========================================================================
# nova classify
# ===========================================================================


def test_classify_run_from_flags_text() -> None:
    result = runner.invoke(
        classify_app,
        ["run", "--name", "Helpdesk", "--domain", "customer_support", "--context", "saas"],
    )
    assert result.exit_code == 0, result.output
    assert "Risk Classification" in result.output


def test_classify_run_missing_required_exit_2() -> None:
    result = runner.invoke(classify_app, ["run", "--name", "X"])
    assert result.exit_code == 2
    assert_flag_in_help(result, "--domain")


def test_classify_run_from_yaml_json_output(tmp_path: Path) -> None:
    sysfile = tmp_path / "system.yaml"
    sysfile.write_text(
        yaml.safe_dump(
            {
                "name": "Yaml System",
                "description": "a system described in yaml",
                "use_case_domain": "general",
                "deployment_context": "internal",
            }
        )
    )
    result = runner.invoke(
        classify_app,
        ["run", "--input", str(sysfile), "--name", "Overridden", "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "eu_ai_act_tier" in data


def test_classify_run_input_file_not_found_exit_2(tmp_path: Path) -> None:
    result = runner.invoke(
        classify_app, ["run", "--input", str(tmp_path / "missing.yaml")]
    )
    assert result.exit_code == 2
    assert "not found" in result.output.lower()


def test_classify_run_input_not_mapping_exit_2(tmp_path: Path) -> None:
    badfile = tmp_path / "bad.yaml"
    badfile.write_text("- just\n- a\n- list\n")
    result = runner.invoke(classify_app, ["run", "--input", str(badfile)])
    assert result.exit_code == 2
    assert "mapping" in result.output.lower()


def test_classify_run_prohibited_exit_1() -> None:
    result = runner.invoke(
        classify_app,
        [
            "run",
            "--name",
            "SocialScore",
            "--domain",
            "government",
            "--context",
            "social credit citizen rating",
            "--description",
            "social credit citizen rating system",
            "--affects-rights",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "PROHIBITED" in result.output.upper()


def test_classify_list_vocabularies() -> None:
    result = runner.invoke(classify_app, ["list-vocabularies"])
    assert result.exit_code == 0, result.output
    assert "eu-ai-act" in result.output


def test_classify_from_capsule_success(tmp_path: Path) -> None:
    data_dir = tmp_path / "novafabric-data"
    cap = data_dir / "capsules" / "cap-1"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text(
        yaml.safe_dump(
            {
                "run_id": "cap-1",
                "asset": {
                    "name": "Captured Agent",
                    "description": "a captured agent",
                    "domain": "general",
                },
            }
        )
    )
    result = runner.invoke(
        classify_app, ["from-capsule", "cap-1", "--data-dir", str(data_dir), "--json"]
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["classification_capsule_id"] == "cap-1"


def test_classify_from_capsule_missing_dir_exit_2(tmp_path: Path) -> None:
    result = runner.invoke(
        classify_app, ["from-capsule", "nope", "--data-dir", str(tmp_path / "dd")]
    )
    assert result.exit_code == 2
    assert "not found" in result.output.lower()


def test_classify_from_capsule_missing_manifest_exit_2(tmp_path: Path) -> None:
    data_dir = tmp_path / "novafabric-data"
    cap = data_dir / "capsules" / "cap-x"
    cap.mkdir(parents=True)
    result = runner.invoke(
        classify_app, ["from-capsule", "cap-x", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 2
    assert "capsule.yaml" in result.output


def test_classify_from_capsule_manifest_not_mapping_exit_2(tmp_path: Path) -> None:
    data_dir = tmp_path / "novafabric-data"
    cap = data_dir / "capsules" / "cap-y"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("- not\n- a\n- mapping\n")
    result = runner.invoke(
        classify_app, ["from-capsule", "cap-y", "--data-dir", str(data_dir)]
    )
    assert result.exit_code == 2
    assert "mapping" in result.output.lower()


# ===========================================================================
# nova redact / subject-proof
# ===========================================================================


def test_redact_rescan_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    cap = _make_capsule(tmp_path / "cap")
    result = runner.invoke(_redact_app(), ["redact", str(cap)])
    assert result.exit_code == 0, result.output
    assert "re-redacted" in result.output
    assert (cap / "redaction-proof.json").exists()


def test_redact_mark_unsafe_skip_requires_rationale(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path / "cap")
    result = runner.invoke(
        _redact_app(), ["redact", str(cap), "--mark-unsafe-skip", "f1"]
    )
    assert result.exit_code == 1
    assert "rationale" in result.output.lower()


def test_redact_metadata_only_missing_proof_exit_1(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump({"run_id": "r"}))
    # clear_unsafe_skips with no overrides/review => metadata-only path
    result = runner.invoke(_redact_app(), ["redact", str(cap), "--clear-unsafe-skips"])
    assert result.exit_code == 1
    assert "missing redaction-proof.json" in result.output


def test_redact_metadata_only_clear_unsafe_skips(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump({"run_id": "r"}))
    proof = {"findings": [], "unsafe_skips": [{"finding_id": "x"}]}
    (cap / "redaction-proof.json").write_text(json.dumps(proof))
    result = runner.invoke(_redact_app(), ["redact", str(cap), "--clear-unsafe-skips"])
    assert result.exit_code == 0, result.output
    updated = json.loads((cap / "redaction-proof.json").read_text())
    assert updated["unsafe_skips"] == []


def test_redact_metadata_only_mark_unknown_finding_exit_1(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump({"run_id": "r"}))
    proof = {"findings": [{"finding_id": "f-known", "rule_id": "rule-1"}]}
    (cap / "redaction-proof.json").write_text(json.dumps(proof))
    result = runner.invoke(
        _redact_app(),
        ["redact", str(cap), "--mark-unsafe-skip", "f-missing", "--rationale", "fp"],
    )
    assert result.exit_code == 1
    assert "not present" in result.output


def test_redact_metadata_only_mark_known_finding_success(tmp_path: Path) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump({"run_id": "r"}))
    proof = {"findings": [{"finding_id": "f-known", "rule_id": "rule-1"}]}
    (cap / "redaction-proof.json").write_text(json.dumps(proof))
    result = runner.invoke(
        _redact_app(),
        [
            "redact",
            str(cap),
            "--mark-unsafe-skip",
            "f-known",
            "--rationale",
            "false positive",
        ],
    )
    assert result.exit_code == 0, result.output
    updated = json.loads((cap / "redaction-proof.json").read_text())
    assert any(s["finding_id"] == "f-known" for s in updated["unsafe_skips"])


def test_redact_review_without_tty_exit_2(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path / "cap")
    # CliRunner stdin is not a TTY, so --review must fail with exit 2.
    result = runner.invoke(_redact_app(), ["redact", str(cap), "--review"])
    assert result.exit_code == 2
    assert "interactive TTY" in result.output


def test_redact_capsule_missing_run_id_bad_param(tmp_path: Path) -> None:
    """A re-scan capsule whose capsule.yaml lacks a string run_id -> BadParameter."""
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(yaml.safe_dump({"not_run_id": "x"}))
    (cap / "model-calls.jsonl").write_text(
        json.dumps({"event_type": "ModelCallCompleted", "agent_id": "a"}) + "\n"
    )
    # No metadata-only flags -> goes to the re-scan path which reads run_id.
    result = runner.invoke(_redact_app(), ["redact", str(cap)])
    assert result.exit_code == 2
    assert "run_id" in result.output


def test_redact_metadata_mark_real_finding_unsafe_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First redact to create the proof, then metadata-only mark a real finding."""
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    cap = _make_capsule(tmp_path / "cap")

    first = runner.invoke(_redact_app(), ["redact", str(cap)])
    assert first.exit_code == 0, first.output
    proof = json.loads((cap / "redaction-proof.json").read_text())
    assert proof["findings"], "expected a PII finding"
    fid = proof["findings"][0]["finding_id"]

    # Metadata-only path (only --mark-unsafe-skip / --rationale) reads the
    # existing proof's findings, so the finding_id is present.
    result = runner.invoke(
        _redact_app(),
        ["redact", str(cap), "--mark-unsafe-skip", fid, "--rationale", "ack"],
    )
    assert result.exit_code == 0, result.output
    written = json.loads((cap / "redaction-proof.json").read_text())
    assert any(s["finding_id"] == fid for s in written.get("unsafe_skips", []))


def test_redact_rescan_mark_missing_finding_exit_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-scan path with --strategy-override + a missing finding id -> exit 1 (354-359)."""
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    cap = _make_capsule(tmp_path / "cap")
    result = runner.invoke(
        _redact_app(),
        [
            "redact",
            str(cap),
            "--strategy-override",
            "any_rule:mask",
            "--mark-unsafe-skip",
            "definitely-not-a-real-finding",
            "--rationale",
            "x",
        ],
    )
    assert result.exit_code == 1
    assert "not present in re-scan proof" in result.output


def test_try_seal_proof_report_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed seal config makes _try_seal_proof_report warn and return (165-170)."""
    from novafabric.cli.redact import _try_seal_proof_report

    bad_cfg = tmp_path / "novaseal.yaml"
    bad_cfg.write_text("profile: local\nkey_path: /nonexistent/k\ncert_path: : : :\n")
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(bad_cfg))
    report = tmp_path / "r.json"
    report.write_bytes(b"{}")
    # Must not raise; either SealConfigError or generic Exception branch.
    _try_seal_proof_report(report, b"{}")
    assert not report.with_suffix(".seal.json").exists()


def test_redact_strategy_override_bad_format_exit_2(tmp_path: Path) -> None:
    cap = _make_capsule(tmp_path / "cap")
    result = runner.invoke(
        _redact_app(), ["redact", str(cap), "--strategy-override", "no-colon-here"]
    )
    assert result.exit_code == 2  # BadParameter


def test_redact_rescan_with_override_carries_unsafe_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-scan path with --strategy-override carries over prior unsafe_skips."""
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    cap = _make_capsule(tmp_path / "cap")
    # First scan to discover a real finding + rule_id and seed an unsafe_skip.
    first = runner.invoke(_redact_app(), ["redact", str(cap)])
    assert first.exit_code == 0, first.output
    proof = json.loads((cap / "redaction-proof.json").read_text())
    assert proof["findings"], "expected at least one PII finding"
    rule_id = proof["findings"][0]["rule_id"]
    proof["unsafe_skips"] = [{"finding_id": "prior", "rule_id": rule_id}]
    (cap / "redaction-proof.json").write_text(json.dumps(proof))

    # Re-scan with an override forces the non-metadata branch; prior unsafe_skips
    # are carried because --clear-unsafe-skips is not passed.
    result = runner.invoke(
        _redact_app(), ["redact", str(cap), "--strategy-override", f"{rule_id}:mask"]
    )
    assert result.exit_code == 0, result.output
    updated = json.loads((cap / "redaction-proof.json").read_text())
    assert any(s["finding_id"] == "prior" for s in updated.get("unsafe_skips", []))


# --- subject-proof ----------------------------------------------------------


def test_subject_proof_no_pepper_exit_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVA_PII_PEPPER", raising=False)
    result = runner.invoke(_redact_app(), ["subject-proof", "alice@example.com"])
    assert result.exit_code == 1
    assert "NOVA_PII_PEPPER" in result.output


def test_subject_proof_no_db_legal_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    db = tmp_path / "missing.db"
    result = runner.invoke(
        _redact_app(), ["subject-proof", "alice@example.com", "--db", str(db)]
    )
    assert result.exit_code == 0, result.output
    report = _extract_json_obj(result.output)
    assert report["legal_hold_mode"] is True
    assert report["records"] == []


def test_subject_proof_with_records_and_output_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)

    from novafabric.cli.redact import _get_pepper, _hmac_subject
    from novafabric.compliance.pii.index import RedactionSubjectIndex

    subject = "alice@example.com"
    pepper = _get_pepper()
    shmac = _hmac_subject(pepper, subject)

    db = tmp_path / "idx.db"
    idx = RedactionSubjectIndex(db_path=db)
    with idx:
        idx.record(
            subject_id_hmac=shmac,
            capsule_id="cap-1",
            field_path="model-calls.jsonl",
            legal_basis="gdpr_art17",
            redacted_at_utc="2026-01-01T00:00:00Z",
        )

    out = tmp_path / "proof.json"
    result = runner.invoke(
        _redact_app(),
        ["subject-proof", subject, "--db", str(db), "--output", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    written = json.loads(out.read_text())
    assert len(written["records"]) == 1
    assert written["records"][0]["capsule_id"] == "cap-1"


def test_subject_proof_db_default_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting --db derives db_path from NOVAFABRIC_HOME (lines 90-91)."""
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    result = runner.invoke(_redact_app(), ["subject-proof", "carol@example.com"])
    assert result.exit_code == 0, result.output
    report = _extract_json_obj(result.output)
    # No index file under the default home -> legal-hold, empty records.
    assert report["records"] == []


def test_subject_proof_signing_failure_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-ed25519 key triggers the signing-failed warning (138-139) but exits 0."""
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    bad_key = tmp_path / "bad.pem"
    bad_key.write_text("-----BEGIN PRIVATE KEY-----\nnonsense\n-----END PRIVATE KEY-----\n")
    db = tmp_path / "missing.db"
    result = runner.invoke(
        _redact_app(),
        ["subject-proof", "dave@example.com", "--db", str(db), "--key", str(bad_key)],
    )
    assert result.exit_code == 0, result.output
    assert "signing failed" in result.output.lower()


def test_subject_proof_compliance_import_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """compliance module unavailable -> exit 1 (lines 82-84)."""
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    with mock.patch.dict(sys.modules, {"novafabric.compliance.pii.index": None}):
        result = runner.invoke(_redact_app(), ["subject-proof", "eve@example.com"])
    assert result.exit_code == 1
    assert "compliance module not available" in result.output


def test_subject_proof_seals_report_when_novaseal_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a NovaSeal config, the proof report gets a .seal.json (176-188)."""
    import datetime as _dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SealTest")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + _dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\nkey_path: {key_path}\ncert_path: {cert_path}\n"
        f"tsa_url: \nmerkle_db: {tmp_path / 'merkle.db'}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(config_path))

    out = tmp_path / "proof.json"
    db = tmp_path / "missing.db"
    result = runner.invoke(
        _redact_app(),
        ["subject-proof", "frank@example.com", "--db", str(db), "--output", str(out)],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    seal = out.with_suffix(".seal.json")
    assert seal.exists(), result.output


def test_subject_proof_with_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_PII_PEPPER", "unit-test-pepper")
    monkeypatch.delenv("NOVAFABRIC_SEAL_CONFIG", raising=False)
    key = tmp_path / "signer.pem"
    _gen_ed25519_key(key)
    db = tmp_path / "missing.db"  # no records, legal-hold path is fine
    out = tmp_path / "signed_proof.json"
    result = runner.invoke(
        _redact_app(),
        [
            "subject-proof",
            "bob@example.com",
            "--db",
            str(db),
            "--key",
            str(key),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text())
    assert "signature" in report
    assert report["signature"]["sig"]
