"""A required capsule file that is present but unusable is an error, never empty output.

``read_capsule_text_or_none`` returns ``None`` for a missing file *and* for a
link, FIFO, directory or over-size file. That is right for a best-effort
listing and wrong for any reader whose output claims to be complete: the RoPA,
AI-BOM and NIST RMF exporters produced an empty export with exit 0 when
``capsule.yaml`` was a directory (fixed in b8d70e55). This module pins the rest
of that class.

Every reader below (an export, report, compliance artifact, verdict,
attestation, replay, migration, lineage write or PROV-O mapping) gets its
required file replaced by three shapes:

* ``dir``: a directory of the same name;
* ``file-link``: a symlink to a regular file *outside* the capsule holding the
  original content (a reader that followed it would even succeed);
* ``fifo``: a named pipe (opened non-blocking; the test is also time-bounded and
  memory-capped, so a regression fails instead of hanging the machine).

Each must raise :class:`~novafabric._capsule_fs.CapsuleRequiredFileError` (or
the site's own named refusal) and write no output artifact; the same reader on
the clean capsule must still succeed. ``nova`` commands must exit non-zero with
an ``error:`` line, never a traceback and never exit 0.
"""

from __future__ import annotations

import json
import os
import resource
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from novafabric._capsule_fs import (
    CapsuleEntryError,
    CapsuleRequiredFileError,
    read_required_capsule_file,
    read_required_capsule_member,
    read_required_capsule_text,
)

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="POSIX links and FIFOs"),
    pytest.mark.timeout(60),
]

RUN = "01J9ZZREQUIREDFAILCLOSED01"
MODEL = "gpt-required-model"
CREATED = "2026-01-01T00:00:00Z"
SHAPES = ("dir", "file-link", "fifo")


# -- capsule fixture -----------------------------------------------------------------


def _record(i: int) -> dict[str, object]:
    return {
        "model_call_id": f"mc-{i}",
        "tool_call_id": f"tc-{i}",
        "call_id": f"call-{i}",
        "span_id": f"span-{i}",
        "name": "step",
        "tool_name": "real_tool",
        "gen_ai.system": "openai",
        "gen_ai.request.model": MODEL,
        "gen_ai.response.model": MODEL,
        "gen_ai.usage.input_tokens": 3,
        "gen_ai.usage.output_tokens": 4,
        "started_at": "2099-01-01T00:00:00Z",
        "timestamp": "2099-01-01T00:00:00Z",
        "status": "ok",
        "arguments": {"q": "x"},
        "output": {"ok": True},
    }


_LINEAGE_EDGE = {
    "schema_version": "0.4.0",
    "edge_id": "e-1",
    "edge_type": "consumed",
    "source": {
        "kind": "artifact",
        "artifact_ref": {"capsule_run_id": RUN, "path": "inputs/data.csv"},
        "ref": {"path": "inputs/data.csv"},
    },
    "target": {"kind": "run", "run_id": RUN, "ref": {"run_id": RUN}},
    "direction": "source_to_target",
    "created_at": CREATED,
    "emitter": {"name": "novafabric", "version": "0.0.0"},
    "source_run_id": RUN,
    "target_run_id": RUN,
    "capsule_run_id": RUN,
    "emitted_at": CREATED,
}


def _write_capsule(cap: Path) -> Path:
    (cap / "inputs").mkdir(parents=True)
    (cap / "outputs").mkdir()
    (cap / "inputs" / "data.csv").write_text("a,b\n1,2\n")
    manifest = {
        "run_id": RUN,
        "status": "success",
        "exit_code": 0,
        "created_at": CREATED,
        "finished_at": "2026-01-01T00:00:01Z",
        "capture_mode": "cli-wrapper",
        "command": ["python", "agent.py"],
        "model_call_count": 1,
        "tool_call_count": 1,
        "outputs": [{"name": "result.txt", "path": "outputs/result.txt"}],
    }
    (cap / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    (cap / "capsule.json").write_text(json.dumps({"run_id": RUN}))
    line = json.dumps(_record(1)) + "\n"
    for name in ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl", "events.jsonl"):
        (cap / name).write_text(line)
    (cap / "assets.jsonl").write_text("")
    (cap / "produced.jsonl").write_text("")
    (cap / "lineage.jsonl").write_text(json.dumps(_LINEAGE_EDGE) + "\n")
    (cap / "replay.yaml").write_text(yaml.safe_dump({"schema_version": "0.1.0", "mode": "mocked"}))
    (cap / "env.lock").write_text(yaml.safe_dump({"mode": "best-effort"}))
    (cap / "eval_result.json").write_text(json.dumps({"suite_id": "s", "passed": True}))
    (cap / "redaction-proof.json").write_text(json.dumps({
        "capsule_run_id": RUN,
        "secrets_found": 0,
        "findings": [],
        "findings_count": {"total": 0, "by_severity": {}},
    }))
    (cap / "energy-receipts.jsonl").write_text("")
    return cap


def _plant(shape: str, cap: Path, target: str, outside: Path) -> None:
    path = cap / target
    original = path.read_bytes() if path.is_file() else b""
    path.unlink()
    if shape == "dir":
        path.mkdir()
    elif shape == "file-link":
        copy = outside / f"copy-of-{target}"
        copy.write_bytes(original)
        path.symlink_to(copy)
    elif shape == "fifo":
        os.mkfifo(path)
    else:  # pragma: no cover - parametrisation guard
        raise AssertionError(shape)


@pytest.fixture
def memory_cap() -> Iterator[None]:
    """Cap the address space so a reader stuck on a special file fails, not swaps."""
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    with open("/proc/self/statm") as fh:
        pages = int(fh.read().split()[0])
    cap = pages * os.sysconf("SC_PAGE_SIZE") + 1_500_000_000
    if hard != resource.RLIM_INFINITY:
        cap = min(cap, hard)
    resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


# -- the readers ---------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One class-(A) reader: how to run it, and the files it must not read past."""

    run: Callable[[Path, Path], object]
    targets: tuple[str, ...]
    #: Output artifact (relative to the work dir) that must not exist after a refusal.
    output: str | None = None
    #: Named refusals besides CapsuleRequiredFileError (e.g. the site's own error).
    also: tuple[type[Exception], ...] = ()


def _replay(cap: Path, work: Path) -> object:
    from novafabric.replay._engine import ReplayEngine
    from novafabric.replay._flags import ReplayFlags

    return ReplayEngine(cap, ReplayFlags(mode="forensic"), base_dir=work / "replays").run()


def _attribute(cap: Path, work: Path) -> object:
    from novafabric.diagnose.attribution import attribute_failure

    return attribute_failure(cap)


def _causal(cap: Path, work: Path) -> object:
    from novafabric.diagnose.causal_graph import causal_root_candidates

    return causal_root_candidates(cap)


def _claims(cap: Path, work: Path) -> object:
    from novafabric.diagnose.claim_audit import audit_claims

    return audit_claims(cap)


def _system_card(cap: Path, work: Path) -> object:
    from novafabric.compliance.system_card import SystemCardGenerator

    return SystemCardGenerator().build_card(cap)


def _euaiact_log(cap: Path, work: Path) -> object:
    from novafabric.compliance.euaiact import EuAiActExporter

    records = EuAiActExporter(cap.parent).export()
    assert records, "the clean capsule must be listed"
    return records


def _art12(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.euaiact_art12 import build_art12_report

    return build_art12_report(cap, deployment_id="dep-1")


def _prov_json(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.prov_json import export_prov_json

    return export_prov_json(cap)


def _capsule_ref(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.provenance import build_capsule_ref

    ref = build_capsule_ref(cap, CREATED)
    assert ref is not None
    return ref


def _ro_crate(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.ro_crate import export_ro_crate

    return export_ro_crate(cap, work / "crate.zip")


def _hipaa(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.hipaa_safe_harbor import HIPAASafeHarborExporter

    return HIPAASafeHarborExporter().build_proof(cap)


def _nis2(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.nis2 import NIS2Exporter

    return NIS2Exporter().build_nis2_report("INC-1", cap)


def _aibom(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.aibom import AIBOMExporter

    return AIBOMExporter().build_aibom(cap)


def _ropa(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter

    return GDPRRoPAExporter().build_ropa_entry(cap)


def _nist(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter

    return NISTAIRMFReporter().build_report(cap)


def _bagit(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.examiner import BagItExporter

    return BagItExporter(RUN, cap).export(work / "bag")


def _iso42001(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.examiner import ISO42001Exporter

    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2027, 1, 1, tzinfo=timezone.utc)
    return ISO42001Exporter(cap.parent, start, end).export(work / "iso.zip")


def _pccp(cap: Path, work: Path) -> object:
    from novafabric.compliance.export.examiner import PCCPExporter

    return PCCPExporter(RUN, RUN, cap.parent).export(work / "pccp.json")


def _lineage_writer(cap: Path, work: Path) -> object:
    from novafabric.lineage._writer import LineageWriter

    return LineageWriter(cap, RUN).infer()


def _lineage_import(cap: Path, work: Path) -> object:
    from novafabric.lineage._importer import index_capsule_lineage

    result = index_capsule_lineage(cap, db_path=work / "lineage.db")
    assert not result.skipped, result
    return result


def _openlineage(cap: Path, work: Path) -> object:
    from novafabric.lineage._openlineage import build_events_from_capsule

    return build_events_from_capsule(cap)


def _provo_manifest(cap: Path, work: Path) -> object:
    from novafabric.kg.spkg.provo_mapping import capsule_manifest_to_provo

    return capsule_manifest_to_provo(cap)


def _provo_lineage(cap: Path, work: Path) -> object:
    from novafabric.kg.spkg.provo_mapping import read_lineage_edges

    edges = read_lineage_edges(cap)
    assert edges
    return edges


def _dataset_card(cap: Path, work: Path) -> object:
    from novafabric.supplychain.dataset_card import transforms_from_capsule

    return transforms_from_capsule(cap)


def _annotation_facts(cap: Path, work: Path) -> object:
    from novafabric.eval.annotation_store import _capsule_facts

    return _capsule_facts(cap)


def _blob_identifier(cap: Path, work: Path) -> object:
    from novafabric.export_blob.packing import capsule_identifier

    assert capsule_identifier(cap) == RUN
    return RUN


def _blob_select(cap: Path, work: Path) -> object:
    from novafabric.export_blob.service import ExportSelectionError, select_capsules

    try:
        selection = select_capsules(root=cap.parent, since="2000-01-01T00:00:00Z")
    except ExportSelectionError as exc:
        raise _Refused(str(exc)) from exc
    assert [d.name for d in selection.capsule_dirs] == [RUN]
    return selection


def _blob_unsafe_skips(cap: Path, work: Path) -> object:
    from novafabric.export_blob.service import _check_unsafe_skips

    return _check_unsafe_skips(cap)


def _energy_attest(cap: Path, work: Path) -> object:
    from novafabric.energy._attribution import attest_capsule

    return attest_capsule(cap, rapl_base=work / "no-rapl", node_id="n1")


def _energy_receipts(cap: Path, work: Path) -> object:
    from novafabric.energy._attribution import load_receipts

    return load_receipts(cap)


def _completeness(cap: Path, work: Path) -> object:
    from novafabric.evidence.completeness import compute_completeness

    return compute_completeness(cap, RUN)


def _replay_pins(cap: Path, work: Path) -> object:
    from novafabric.evidence.replay_attestation import pinned_block_from_capsule

    block = pinned_block_from_capsule(cap)
    assert block.model.request_model == MODEL
    return block


def _c2pa(cap: Path, work: Path) -> object:
    from novafabric.evidence.c2pa_exporter import C2PAManifestExporter

    return C2PAManifestExporter().build_manifest(cap)


def _assure(cap: Path, work: Path) -> object:
    from novafabric.assure.checker import AssuranceChecker

    return AssuranceChecker().check_all(cap)


def _offline_contract(cap: Path, work: Path) -> object:
    from novafabric.eval.offline import run_contract

    return run_contract(cap, {"type": "object"}, field="output")


def _offline_coverage(cap: Path, work: Path) -> object:
    from novafabric.eval.offline import run_coverage

    return run_coverage(cap, ["real_tool"])


def _fair_manifest(cap: Path, work: Path) -> object:
    from novafabric.science.fair_rocrate import _read_manifest

    return _read_manifest(cap)


def _intoto(cap: Path, work: Path) -> object:
    from novafabric.envelopes.intoto import capsule_statement

    statement = capsule_statement(cap)
    assert statement["predicate"]["runId"] == RUN
    return statement


def _distributed_manifest(cap: Path, work: Path) -> object:
    from novafabric.capsule.validator import DistributedCapsuleValidator

    return DistributedCapsuleValidator(cap)._read_manifest()


def _logical_calls(cap: Path, work: Path) -> object:
    from novafabric.capture.record_roles import (
        count_logical_model_calls_in_file,
        read_logical_model_calls,
    )

    assert count_logical_model_calls_in_file(cap / "model-calls.jsonl", required=True) == 1
    return read_logical_model_calls(cap / "model-calls.jsonl", required=True)


class _Refused(Exception):
    """A site's own named refusal, re-raised by its runner for the table."""


_STREAMS = ("model-calls.jsonl", "tool-calls.jsonl")

SITES: dict[str, Site] = {
    "replay": Site(
        _replay,
        ("capsule.yaml", "replay.yaml", "env.lock", *_STREAMS),
        output="replays",
    ),
    "diagnose-attribution": Site(_attribute, ("capsule.yaml", "trace.jsonl", *_STREAMS)),
    "diagnose-causal-graph": Site(_causal, ("capsule.yaml",)),
    "diagnose-claim-audit": Site(_claims, ("capsule.yaml",)),
    "system-card": Site(_system_card, ("capsule.yaml", "eval_result.json", "lineage.jsonl")),
    "euaiact-log": Site(_euaiact_log, ("capsule.yaml", *_STREAMS)),
    "euaiact-art12": Site(_art12, ("capsule.yaml",)),
    "prov-json": Site(_prov_json, ("capsule.yaml", "lineage.jsonl")),
    "provenance-ref": Site(_capsule_ref, ("capsule.yaml", *_STREAMS)),
    "ro-crate": Site(_ro_crate, ("capsule.yaml",), output="crate.zip"),
    "hipaa": Site(_hipaa, ("capsule.yaml",)),
    "nis2": Site(_nis2, ("capsule.yaml",)),
    "aibom": Site(_aibom, ("capsule.yaml", *_STREAMS)),
    "gdpr-ropa": Site(_ropa, ("capsule.yaml",)),
    "nist-rmf": Site(_nist, ("capsule.yaml",)),
    # BagIt refuses every link in the capsule up front (CapsuleLinkError).
    "examiner-bagit": Site(
        _bagit, ("capsule.yaml",), output=f"bag/{RUN}-bag.zip", also=(CapsuleEntryError,)
    ),
    "examiner-iso42001": Site(_iso42001, ("capsule.yaml",), output="iso.zip"),
    "examiner-pccp": Site(_pccp, ("capsule.yaml",), output="pccp.json"),
    "lineage-writer": Site(_lineage_writer, ("capsule.yaml", "assets.jsonl", "produced.jsonl")),
    "lineage-import": Site(_lineage_import, ("lineage.jsonl",)),
    "openlineage": Site(_openlineage, ("capsule.yaml", "lineage.jsonl")),
    "provo-manifest": Site(_provo_manifest, ("capsule.yaml",)),
    "provo-lineage": Site(_provo_lineage, ("lineage.jsonl",)),
    "dataset-card": Site(_dataset_card, ("lineage.jsonl",)),
    "annotation-facts": Site(_annotation_facts, ("capsule.yaml", "tool-calls.jsonl")),
    "export-blob-identifier": Site(_blob_identifier, ("capsule.yaml",)),
    "export-blob-select": Site(_blob_select, ("capsule.yaml",), also=(_Refused,)),
    "export-blob-unsafe-skips": Site(_blob_unsafe_skips, ("redaction-proof.json",)),
    "energy-attest": Site(_energy_attest, ("capsule.yaml", *_STREAMS)),
    "energy-receipts": Site(_energy_receipts, ("energy-receipts.jsonl",)),
    "evidence-completeness": Site(
        _completeness, ("capsule.yaml", "trace.jsonl", "lineage.jsonl", *_STREAMS)
    ),
    "replay-attestation-pins": Site(_replay_pins, ("env.lock", "model-calls.jsonl")),
    "c2pa": Site(_c2pa, ("capsule.yaml", "model-calls.jsonl")),
    "assure": Site(
        _assure,
        ("capsule.yaml", "redaction-proof.json", "replay.yaml", "trace.jsonl", *_STREAMS),
    ),
    "eval-offline-contract": Site(_offline_contract, ("tool-calls.jsonl",)),
    "eval-offline-coverage": Site(_offline_coverage, ("tool-calls.jsonl",)),
    "fair-rocrate-manifest": Site(_fair_manifest, ("capsule.yaml",)),
    "intoto-statement": Site(_intoto, ("capsule.yaml",)),
    "distributed-validator": Site(_distributed_manifest, ("capsule.json",)),
    "record-roles-required": Site(_logical_calls, ("model-calls.jsonl",)),
}

CASES = [
    pytest.param(name, target, shape, id=f"{name}-{target}-{shape}")
    for name, site in sorted(SITES.items())
    for target in site.targets
    for shape in SHAPES
]


@pytest.mark.parametrize(("name", "target", "shape"), CASES)
def test_unusable_required_file_is_an_error(
    name: str, target: str, shape: str, tmp_path: Path, memory_cap: None
) -> None:
    site = SITES[name]
    cap = _write_capsule(tmp_path / "caps" / RUN)
    outside = tmp_path / "outside"
    outside.mkdir()
    _plant(shape, cap, target, outside)
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises((CapsuleRequiredFileError, *site.also)) as info:
        site.run(cap, work)
    if isinstance(info.value, CapsuleRequiredFileError):
        assert info.value.rel == target
        assert target in str(info.value)
    if site.output is not None:
        out = work / site.output
        assert not out.exists() or (out.is_dir() and not any(out.iterdir())), (
            f"{name} wrote {site.output} despite the refusal"
        )


@pytest.mark.parametrize("name", sorted(SITES))
def test_clean_capsule_still_works(name: str, tmp_path: Path) -> None:
    cap = _write_capsule(tmp_path / "caps" / RUN)
    work = tmp_path / "work"
    work.mkdir()
    SITES[name].run(cap, work)


@pytest.mark.parametrize("name", sorted(SITES))
def test_absent_optional_files_are_still_no_data(name: str, tmp_path: Path) -> None:
    """Only *present but unusable* is an error: an absent stream stays "no data"."""
    cap = _write_capsule(tmp_path / "caps" / RUN)
    for optional in ("eval_result.json", "produced.jsonl", "energy-receipts.jsonl", "env.lock"):
        (cap / optional).unlink()
    work = tmp_path / "work"
    work.mkdir()
    SITES[name].run(cap, work)


@pytest.mark.parametrize("shape", SHAPES)
def test_trust_radar_fails_an_unusable_redaction_proof(
    shape: str, tmp_path: Path, memory_cap: None
) -> None:
    """A verdict surface: present-but-unusable fails both axes, never n/a or clean."""
    from novafabric.trust.capsule_flags import _redaction_flags

    cap = _write_capsule(tmp_path / RUN)
    assert _redaction_flags(cap) == {"secret_scan_clean": True, "redaction_coverage": 1.0}
    _plant(shape, cap, "redaction-proof.json", tmp_path)
    assert _redaction_flags(cap) == {"secret_scan_clean": False, "redaction_coverage": 0.0}
    proof = cap / "redaction-proof.json"
    if shape == "dir":
        proof.rmdir()
    else:
        proof.unlink()
    assert _redaction_flags(cap) == {}  # absent: n/a, as before


def test_annotation_populate_names_the_unreadable_capsule(tmp_path: Path) -> None:
    from novafabric.eval.annotation_queue import AnnotationError
    from novafabric.eval.annotation_store import create_queue, populate_queue
    from novafabric.eval.score_config import ScoreRange, ScoreValueType
    from novafabric.eval.score_config_catalog import register_config

    db = tmp_path / "registry.db"
    register_config(
        "factuality", ScoreValueType.NUMERIC, "Higher is better.",
        range_=ScoreRange(min=0, max=1), db_path=db,
    )
    create_queue(name="q1", criteria=["factuality"], db_path=db)
    base = tmp_path / "caps"
    cap = _write_capsule(base / RUN)
    assert populate_queue("q1", base, db_path=db, dry_run=True)["matched"] == 1
    _plant("fifo", cap, "capsule.yaml", tmp_path)
    with pytest.raises(AnnotationError, match=RUN):
        populate_queue("q1", base, db_path=db)


# -- the helper itself ---------------------------------------------------------------


def test_helper_returns_none_only_for_an_absent_file(tmp_path: Path) -> None:
    cap = _write_capsule(tmp_path / RUN)
    assert read_required_capsule_text(cap, "absent.jsonl", operation="t") is None
    assert read_required_capsule_member(cap / "absent.jsonl", operation="t") is None
    assert read_required_capsule_file(tmp_path / "no-such-capsule", "x", operation="t") is None
    assert read_required_capsule_text(cap, "capsule.yaml", operation="t") is not None


@pytest.mark.parametrize("shape", [*SHAPES, "dangling-link", "dotdot", "too-big"])
def test_helper_raises_for_present_but_unusable(shape: str, tmp_path: Path) -> None:
    cap = _write_capsule(tmp_path / RUN)
    rel = "model-calls.jsonl"
    kwargs: dict[str, object] = {}
    if shape == "dangling-link":
        (cap / rel).unlink()
        (cap / rel).symlink_to(tmp_path / "gone")
    elif shape == "dotdot":
        rel = "../outside.jsonl"
        (tmp_path / "outside.jsonl").write_text("{}\n")
    elif shape == "too-big":
        kwargs["max_bytes"] = 4
    else:
        _plant(shape, cap, rel, tmp_path)
    with pytest.raises(CapsuleRequiredFileError) as info:
        read_required_capsule_text(cap, rel, operation="the test", **kwargs)  # type: ignore[arg-type]
    assert "the test refuses" in str(info.value)


def test_helper_logs_the_refusal(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    cap = _write_capsule(tmp_path / RUN)
    _plant("dir", cap, "capsule.yaml", tmp_path)
    with caplog.at_level("WARNING", logger="novafabric._capsule_fs"):
        with pytest.raises(CapsuleRequiredFileError):
            read_required_capsule_member(cap / "capsule.yaml", operation="t")
    assert [r.message for r in caplog.records] == ["capsule_required_file_unusable"]


# -- the nova CLI --------------------------------------------------------------------


def _invoke(args: list[str]) -> object:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    return CliRunner().invoke(app, args)


@dataclass(frozen=True)
class Command:
    args: Callable[[Path, Path], list[str]]
    targets: tuple[str, ...]
    #: Exit code on refusal. 2 = the root-group handler (an ``error:`` line).
    code: int = 2
    output: str | None = None
    #: Text the refusal must print (besides the file name).
    says: str = "error:"


COMMANDS: dict[str, Command] = {
    # (an unusable capsule.yaml is refused earlier by the capsule-ref resolver,
    # "No capsule at path", exit 1; the engine's own read is pinned above)
    "replay": Command(
        lambda cap, work: ["replay", str(cap), "--mode", "forensic", "--output-dir",
                           str(work / "replays")],
        ("replay.yaml", "env.lock", "model-calls.jsonl"),
        output="replays",
    ),
    "diagnose": Command(
        lambda cap, work: ["diagnose", RUN, "--capsule-dir", str(cap.parent)],
        ("capsule.yaml", "trace.jsonl"),
    ),
    "assure": Command(
        lambda cap, work: ["assure", str(cap), "--format", "json"],
        ("capsule.yaml", "redaction-proof.json"),
    ),
    "cost-estimate": Command(
        lambda cap, work: ["cost", "estimate", str(cap), "--format", "json"],
        ("model-calls.jsonl",),
    ),
    "euaiact-export": Command(
        lambda cap, work: ["euaiact", "export", "--capsules-dir", str(cap.parent),
                           "-o", str(work / "art12.json")],
        ("capsule.yaml", "model-calls.jsonl"),
        output="art12.json",
    ),
    "classify": Command(
        lambda cap, work: ["classify", "from-capsule", RUN, "--data-dir",
                           str(cap.parent.parent)],
        ("capsule.yaml",),
    ),
    "scan-secrets": Command(
        lambda cap, work: ["scan-secrets", str(cap)],
        ("redaction-proof.json",),
    ),
    # nova validate reports it as a validation error (exit 1) and never blocks
    # on a FIFO (it used to read capsule.yaml with Path.read_text()).
    "validate": Command(
        lambda cap, work: ["validate", str(cap)],
        ("capsule.yaml", "env.lock", "lineage.jsonl", "model-calls.jsonl"),
        code=1,
        says="present but",
    ),
}

CLI_CASES = [
    pytest.param(name, target, shape, id=f"{name}-{target}-{shape}")
    for name, cmd in sorted(COMMANDS.items())
    for target in cmd.targets
    for shape in SHAPES
]


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "nova-home"
    home.mkdir()
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    for var in ("NOVAFABRIC_CAPSULE_DIR", "NOVA_DATA_DIR", "NOVAFABRIC_SEAL_DB_PATH",
                "FORCE_COLOR", "COLORTERM"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.mark.parametrize(("name", "target", "shape"), CLI_CASES)
def test_cli_exits_nonzero_with_a_message(
    name: str, target: str, shape: str, tmp_path: Path, isolated_home: Path, memory_cap: None
) -> None:
    cmd = COMMANDS[name]
    cap = _write_capsule(tmp_path / "capsules" / RUN)
    outside = tmp_path / "outside"
    outside.mkdir()
    _plant(shape, cap, target, outside)
    work = tmp_path / "work"
    work.mkdir()
    result = _invoke(cmd.args(cap, work))
    assert isinstance(result.exception, SystemExit), (  # type: ignore[attr-defined]
        f"{name} crashed: {result.exception!r}"  # type: ignore[attr-defined]
    )
    assert result.exit_code == cmd.code, result.output  # type: ignore[attr-defined]
    output = result.output  # type: ignore[attr-defined]
    assert cmd.says in output and target in output, output
    if cmd.output is not None:
        out = work / cmd.output
        assert not out.exists() or (out.is_dir() and not any(out.iterdir())), output


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_cli_clean_capsule_is_not_refused(name: str, tmp_path: Path, isolated_home: Path) -> None:
    cmd = COMMANDS[name]
    cap = _write_capsule(tmp_path / "capsules" / RUN)
    work = tmp_path / "work"
    work.mkdir()
    result = _invoke(cmd.args(cap, work))
    output = result.output  # type: ignore[attr-defined]
    assert "is present but cannot be used" not in output, output
    assert result.exception is None or isinstance(  # type: ignore[attr-defined]
        result.exception, SystemExit  # type: ignore[attr-defined]
    ), output


# -- HTTP surfaces --------------------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_serve_governance_classify_never_answers_from_an_empty_manifest(
    shape: str, tmp_path: Path, isolated_home: Path, memory_cap: None
) -> None:
    from fastapi.testclient import TestClient

    from novafabric.serve.app import create_app

    runs = tmp_path / "runs"
    cap = _write_capsule(runs / RUN)
    client = TestClient(create_app(token="t", capsule_dir=runs, db_path=tmp_path / "r.db"))
    params = {"token": "t", "run_id": RUN}
    host = {"host": "127.0.0.1:4321"}
    assert client.get("/api/governance/classify", params=params, headers=host).status_code == 200
    _plant(shape, cap, "capsule.yaml", tmp_path)
    resp = client.get("/api/governance/classify", params=params, headers=host)
    # 403: the run-resolution guard refused the capsule first; 422: the read did.
    assert resp.status_code in (403, 422), resp.text
    assert str(tmp_path) not in resp.text


@pytest.mark.parametrize("shape", SHAPES)
def test_server_capsule_detail_is_409_not_an_empty_200(
    shape: str, tmp_path: Path, memory_cap: None
) -> None:
    from novafabric.server.errors import ConflictError
    from novafabric.server.routes.capsules import _load_manifest

    cap = _write_capsule(tmp_path / RUN)
    assert _load_manifest(cap)["run_id"] == RUN
    _plant(shape, cap, "capsule.yaml", tmp_path)
    with pytest.raises(ConflictError) as info:
        _load_manifest(cap)
    assert info.value.code == "capsule_file_unusable"
    assert str(tmp_path) not in str(info.value)
