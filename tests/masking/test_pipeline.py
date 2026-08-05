"""Masking pipeline invariants (ADR-0135 D3–D5, pii-masking-pipeline-v0).

The invariants under test:

1. Built-in ADR-0009 rules always run — a masker crash never un-redacts.
2. A crashing / hanging / invalid masker is contained: fail-closed on the
   field, recorded in ``masker_errors[]``, and capture never crashes.
3. A masked value never appears in the capsule; ``match_hash`` attributes
   it without storing the bytes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema

from novafabric.capture.secrets import SecretScannerV0, recompute_chain_hash
from novafabric.masking import MaskerSpec, MaskingPipeline
from novafabric.masking._registry import LoadedMasker

from .conftest import (
    CaseIdMasker,
    CrashingMasker,
    LeakyMasker,
    NoOpMasker,
    NotAStringMasker,
    SleepyMasker,
    write_capsule_files,
)

_SCHEMAS = Path(__file__).parents[2] / "schemas"
_FINDING_SCHEMA = json.loads((_SCHEMAS / "masker-finding.schema.json").read_text())
_ERROR_SCHEMA = json.loads((_SCHEMAS / "masker-error.schema.json").read_text())
_PROOF_SCHEMA = json.loads(
    (
        Path(__file__).parents[2]
        / "src" / "novafabric" / "schemas" / "secret-redaction.schema.json"
    ).read_text()
)


def _pipeline(*maskers: object, **spec_kwargs: object) -> MaskingPipeline:
    # The production default is DEFAULT_TIMEOUT_MS = 50 ms of *wall clock*. On a
    # box running the suite under `-n auto` that budget is regularly missed by a
    # masker that does almost no work, and the pipeline correctly fails closed —
    # turning every `assert errors == []` below into a load-dependent flake
    # (observed on a 24-worker run: "masker 'acme-case-id' timeout on
    # model-calls.jsonl#L1 call_id; field redacted (fail-closed)").
    #
    # None of these tests is about the budget, so they get a generous one.
    # `test_timeout_is_bounded_and_fails_closed` — the one test that *is* about
    # the budget — passes `timeout_ms=50` explicitly and overrides this.
    spec_kwargs.setdefault("timeout_ms", 30_000)
    loaded = [
        LoadedMasker(masker=m, spec=MaskerSpec(id=str(getattr(m, "masker_id")), **spec_kwargs))  # type: ignore[arg-type]
        for m in maskers
    ]
    return MaskingPipeline(loaded)


def _capsule_text(capsule_dir: Path) -> str:
    return "".join(
        (capsule_dir / f).read_text()
        for f in ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl")
        if (capsule_dir / f).exists()
    )


def test_masked_value_never_appears_in_capsule(capsule_dir: Path) -> None:
    findings, errors = _pipeline(CaseIdMasker()).run(capsule_dir, run_id="r1")
    text = _capsule_text(capsule_dir)
    assert "ACME-CASE-12345" not in text
    assert "ACME-CASE-99" not in text
    assert "[MASKED:acme-case-id]" in text
    assert errors == []
    assert len(findings) == 2
    for f in findings:
        jsonschema.validate(f, _FINDING_SCHEMA, format_checker=jsonschema.FormatChecker())
        assert "ACME-CASE" not in f["replacement"]
    # match_hash attributes the pre-mask value without storing it.
    by_ref = {f["target_ref"]: f for f in findings}
    tool_finding = by_ref["tool-calls.jsonl#L1 arguments.case"]
    candidate = "ACME-CASE-99"
    assert tool_finding["match_hash"] == (
        "sha256:" + hashlib.sha256(candidate.encode()).hexdigest()
    )
    assert tool_finding["byte_length"] == len(candidate.encode())
    assert tool_finding["redaction_strategy"] == "mask"
    assert tool_finding["chain_position"] == 0


def test_builtin_rules_always_run_even_when_masker_crashes(tmp_path: Path) -> None:
    """Invariant 1: a crashing plugin never un-redacts the built-in scanner."""
    capsule_dir = tmp_path / "capsule"
    secret = "sk-ant-" + "a" * 40
    write_capsule_files(capsule_dir, secret=secret)

    scanner = SecretScannerV0(capsule_dir=capsule_dir, run_id="r1")
    proof = scanner.scan_and_redact()
    assert proof["findings_count"]["total"] >= 1

    findings, errors = _pipeline(CrashingMasker()).run(capsule_dir, run_id="r1")
    text = _capsule_text(capsule_dir)
    assert secret not in text  # built-in redaction survived the crash
    assert findings == []
    assert errors, "crash must be recorded"
    assert all(e["reason"] == "raised" for e in errors)
    assert all(e["action_taken"] == "redact" for e in errors)
    for e in errors:
        jsonschema.validate(e, _ERROR_SCHEMA, format_checker=jsonschema.FormatChecker())


def test_crashing_masker_is_contained_and_fails_closed(capsule_dir: Path) -> None:
    """Invariant 2: run() never raises; every field it touched is redacted."""
    findings, errors = _pipeline(CrashingMasker()).run(capsule_dir, run_id="r1")
    text = _capsule_text(capsule_dir)
    # fail-closed: the crashing masker's fields were redacted, not left raw
    assert "ACME-CASE-12345" not in text
    assert "[MASKED:crashing-masker]" in text
    assert findings == []
    assert all(e["masker_id"] == "crashing-masker" for e in errors)
    assert all(e["detail_hash"].startswith("sha256:") for e in errors)


def test_timeout_is_bounded_and_fails_closed(capsule_dir: Path) -> None:
    findings, errors = _pipeline(SleepyMasker(), timeout_ms=50).run(capsule_dir, run_id="r1")
    assert findings == []
    assert errors
    assert all(e["reason"] == "timeout" for e in errors)
    assert "[MASKED:sleepy-masker]" in _capsule_text(capsule_dir)


def test_oversize_input_is_aborted_and_fails_closed(capsule_dir: Path) -> None:
    findings, errors = _pipeline(CaseIdMasker(), max_input_bytes=4).run(
        capsule_dir, run_id="r1"
    )
    assert findings == []
    assert errors
    assert all(e["reason"] == "oversize" for e in errors)
    assert "ACME-CASE-12345" not in _capsule_text(capsule_dir)


def test_leaky_masker_output_is_rejected(capsule_dir: Path) -> None:
    """A 'masked' value still containing the raw bytes is declined_invalid."""
    findings, errors = _pipeline(LeakyMasker()).run(capsule_dir, run_id="r1")
    text = _capsule_text(capsule_dir)
    assert "ACME-CASE-12345" not in text
    # No non-empty value may pass through (empty values have no bytes to leak).
    assert all(f["byte_length"] == 0 for f in findings)
    assert errors
    assert all(e["reason"] == "declined_invalid" for e in errors)


def test_non_string_masker_output_is_rejected(capsule_dir: Path) -> None:
    findings, errors = _pipeline(NotAStringMasker()).run(capsule_dir, run_id="r1")
    assert findings == []
    assert all(e["reason"] == "declined_invalid" for e in errors)
    assert "42" not in (capsule_dir / "tool-calls.jsonl").read_text()


def test_noop_masker_produces_no_finding(capsule_dir: Path) -> None:
    before = _capsule_text(capsule_dir)
    findings, errors = _pipeline(NoOpMasker()).run(capsule_dir, run_id="r1")
    assert findings == []
    assert errors == []
    assert _capsule_text(capsule_dir) == before  # byte-for-byte untouched


def test_on_error_drop_empties_the_field(capsule_dir: Path) -> None:
    findings, errors = _pipeline(CrashingMasker(), on_error="drop").run(
        capsule_dir, run_id="r1"
    )
    assert all(e["action_taken"] == "drop" for e in errors)
    line = (capsule_dir / "tool-calls.jsonl").read_text().splitlines()[0]
    obj = json.loads(line)
    assert obj["arguments"]["case"] == ""


def test_maskers_compose_in_declared_order(capsule_dir: Path) -> None:
    """Two maskers touch the same field; the chain is reconstructable."""

    class SecondMasker:
        masker_id = "second-masker"
        masker_version = "1"
        pattern_ids = ("marker",)

        def mask(self, field: object, value: str, context: object) -> object:
            from novafabric.masking import UNCHANGED

            if "[MASKED:acme-case-id]" in value:
                return value.replace("[MASKED:acme-case-id]", "[MASKED:second]")
            return UNCHANGED

    findings, errors = _pipeline(CaseIdMasker(), SecondMasker()).run(
        capsule_dir, run_id="r1"
    )
    assert errors == []
    per_field: dict[str, list[dict[str, object]]] = {}
    for f in findings:
        per_field.setdefault(str(f["target_ref"]), []).append(f)
    chained = per_field["tool-calls.jsonl#L1 arguments.case"]
    assert [f["masker_id"] for f in chained] == ["acme-case-id", "second-masker"]
    assert [f["chain_position"] for f in chained] == [0, 1]
    # Each later masker observed the prior stage's output.
    assert chained[1]["match_hash"] == (
        "sha256:" + hashlib.sha256(str(chained[0]["replacement"]).encode()).hexdigest()
    )
    assert "[MASKED:second]" in _capsule_text(capsule_dir)


def test_extended_proof_validates_and_chain_hash_verifies(tmp_path: Path) -> None:
    """The extended proof passes the packaged redaction-proof schema and its
    chain_hash covers the new arrays."""
    from novafabric.capture._ulid import new_ulid

    run_id = new_ulid()
    capsule_dir = tmp_path / "capsule"
    write_capsule_files(capsule_dir, email="alice@example.com")
    scanner = SecretScannerV0(capsule_dir=capsule_dir, run_id=run_id)
    proof = scanner.scan_and_redact()

    from novafabric.masking.examples import EmailMasker

    findings, errors = _pipeline(EmailMasker()).run(capsule_dir, run_id=run_id)
    proof["masker_findings"] = findings
    proof["masker_errors"] = errors
    proof = recompute_chain_hash(proof)

    jsonschema.validate(proof, _PROOF_SCHEMA, format_checker=jsonschema.FormatChecker())
    assert proof["masker_findings"], "email must have been masked"
    assert "alice@example.com" not in _capsule_text(capsule_dir)

    stored = proof["chain_hash"]
    assert recompute_chain_hash(dict(proof))["chain_hash"] == stored
    # Tamper with a masker finding → the chain hash no longer verifies.
    tampered = json.loads(json.dumps(proof))
    tampered["masker_findings"][0]["masker_id"] = "evil"
    assert recompute_chain_hash(tampered)["chain_hash"] != stored


def test_absent_pipeline_leaves_scanner_output_byte_for_byte(tmp_path: Path) -> None:
    """No maskers configured ⇒ files and proof are exactly ADR-0009."""
    a, b = tmp_path / "a", tmp_path / "b"
    write_capsule_files(a)
    write_capsule_files(b)
    SecretScannerV0(capsule_dir=a, run_id="r1").scan_and_redact()
    SecretScannerV0(capsule_dir=b, run_id="r1").scan_and_redact()
    findings, errors = MaskingPipeline([]).run(b, run_id="r1")
    assert findings == [] and errors == []
    for f in ("model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl"):
        assert (a / f).read_bytes() == (b / f).read_bytes()


def test_unparseable_jsonl_line_is_left_intact(capsule_dir: Path) -> None:
    trace = capsule_dir / "trace.jsonl"
    trace.write_text('{"broken": \n' + trace.read_text())
    findings, errors = _pipeline(CaseIdMasker()).run(capsule_dir, run_id="r1")
    assert '{"broken": ' in trace.read_text()
    assert errors == []  # not a masker failure; scanner already covered raw bytes


def test_yaml_target_is_masked(capsule_dir: Path) -> None:
    import yaml

    (capsule_dir / "capsule.yaml").write_text(
        yaml.dump({"run_id": "r1", "command": "open ACME-CASE-777"})
    )
    findings, errors = _pipeline(CaseIdMasker()).run(capsule_dir, run_id="r1")
    data = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
    assert data["command"] == "open [MASKED:acme-case-id]"
    assert any(f["target_ref"] == "capsule.yaml command" for f in findings)
    assert errors == []


def test_masker_config_reaches_the_masker(capsule_dir: Path) -> None:
    loaded = [
        LoadedMasker(
            masker=CaseIdMasker(),
            spec=MaskerSpec(id="acme-case-id", config={"prefix": "NOPE"}),
        )
    ]
    findings, _ = MaskingPipeline(loaded).run(capsule_dir, run_id="r1")
    assert findings == []  # configured prefix does not match → masker declines
    assert "ACME-CASE-12345" in _capsule_text(capsule_dir)
