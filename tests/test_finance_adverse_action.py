"""ADR-0159 D6 / NF-278 — credit-decision specific-reasons evidence pack and its collector.

Acceptance criteria covered:
- the recorded model call(s) render as model ref + input/output digests, never prompt text;
- ``inputs/`` files render bound to the capsule's sealed ``evidence_digests``;
- the recorded ``facets.feature_attribution`` renders in recorded order with the recorded rank —
  never re-sorted, re-ranked, or computed;
- no attribution facet -> ``principal_reasons`` is ``missing`` with a reason (spec crosswalk);
- truncation / unbound digests / suppressed secret-shaped text / unknown model-call ref ->
  ``partial`` with reasons;
- a forged/mismatched digest or malformed sealed evidence -> ``CorruptCapsuleError``;
- the pack carries the CFPB honesty line and the finance banner, and has no notice text,
  verdict, or credit-outcome field;
- the collector is strictly read-only and never follows symlinks out of the capsule.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml
from _adverse_action_capsule import FIXTURES, PROMPT, SECRET, make_capsule, valid_facet
from _adverse_action_capsule import call as _call
from _adverse_action_capsule import sha as _sha

from novafabric.compliance.export.finance import _sealed_read
from novafabric.compliance.export.finance import adverse_action_collect as collect
from novafabric.compliance.export.finance.adverse_action import (
    ADVERSE_ACTION_REGIME,
    CFPB_HONESTY_LINE,
    FINANCE_HONESTY_BANNER,
    REASON_MODEL_CALLS_UNBOUND,
    REASON_NO_ATTRIBUTION,
    REASON_NO_INPUTS,
    REASON_NO_MODEL_CALL,
    REASON_NO_REASONS,
    REASON_NO_SEAL,
    build_adverse_action_pack,
)
from novafabric.compliance.export.finance.adverse_action_collect import (
    CorruptCapsuleError,
    collect_capsule_facts,
)
from novafabric.compliance.export.provenance import EvidenceSource


def _full(tmp_path: Path) -> Path:
    return make_capsule(
        tmp_path,
        calls=[_call("mc-001")],
        inputs={"application.json": b'{"applicant_ref": "sha256:abc"}'},
        facet=valid_facet(),
    )


def _rows(pack: Any) -> dict[str, Any]:
    return {r.key: r for r in pack.rows}


# --------------------------------------------------------------------------- happy path


def test_complete_capsule_renders_every_row_complete(tmp_path: Path) -> None:
    pack = build_adverse_action_pack(collect_capsule_facts(_full(tmp_path)))
    rows = _rows(pack)
    assert {k: r.status for k, r in rows.items()} == {
        "model_call": "complete",
        "sealed_inputs": "complete",
        "principal_reasons": "complete",
        "seal": "complete",
    }
    assert pack.summary == {"complete": 4, "partial": 0, "missing": 0}
    assert pack.regime == ADVERSE_ACTION_REGIME
    assert pack.banner == FINANCE_HONESTY_BANNER
    assert pack.cfpb_honesty == CFPB_HONESTY_LINE
    assert pack.run_id == "01RUNCREDIT"
    assert pack.run_status == "success"
    assert pack.attribution_method == "shap-tree"
    assert pack.attribution_producer == "credit-underwriter-v4/explainer@1.2"
    assert rows["seal"].source_refs == [".seal/manifest.dsse"]
    assert rows["model_call"].evidence_source is EvidenceSource.operator_asserted
    assert (
        rows["principal_reasons"].source_refs[0].endswith("capsule.yaml#facets.feature_attribution")
    )


def test_reasons_render_in_recorded_order_with_recorded_rank(tmp_path: Path) -> None:
    pack = build_adverse_action_pack(collect_capsule_facts(_full(tmp_path)))
    got = [(r.position, r.rank, r.feature, r.contribution) for r in pack.principal_reasons]
    assert got == [
        (1, 2, "delinquency_count_24m", -0.18),
        (2, 1, "debt_to_income", -0.31),
        (3, 3, "credit_history_months", -0.07),
    ]


def test_model_call_digests_bind_recorded_content_without_copying_it(tmp_path: Path) -> None:
    cap = _full(tmp_path)
    pack = build_adverse_action_pack(collect_capsule_facts(cap))
    (call,) = pack.model_calls
    messages = _call("mc-001")["gen_ai.request.messages"]
    canonical = json.dumps(messages, sort_keys=True, separators=(",", ":")).encode()
    assert call.input_digest == _sha(canonical)
    assert call.output_digest is not None and call.output_digest.startswith("sha256:")
    line = (cap / "model-calls.jsonl").read_bytes().split(b"\n")[0]
    assert call.record_sha256 == _sha(line)
    assert (call.line, call.model_call_id, call.request_model) == (
        1,
        "mc-001",
        "credit-underwriter-v4",
    )
    dumped = pack.model_dump_json()
    assert PROMPT not in dumped and "DENY" not in dumped  # ADR-0021 §4: no prompt/response text


def test_sealed_inputs_are_bound_to_evidence_digests(tmp_path: Path) -> None:
    pack = build_adverse_action_pack(collect_capsule_facts(_full(tmp_path)))
    (inp,) = pack.sealed_inputs
    assert inp.path == "inputs/application.json"
    assert inp.bound is True
    assert inp.sha256 == _sha(b'{"applicant_ref": "sha256:abc"}')


# --------------------------------------------------------------------------- missing / partial


def test_no_attribution_facet_is_missing_with_reason(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], inputs={"a": b"x"})
    pack = build_adverse_action_pack(collect_capsule_facts(cap))
    row = _rows(pack)["principal_reasons"]
    assert row.status == "missing"
    assert row.reasons == [REASON_NO_ATTRIBUTION]
    assert row.source_refs == []
    assert row.evidence_source is EvidenceSource.unverifiable
    assert pack.principal_reasons == []
    assert pack.attribution_method is None


def test_bare_capsule_reports_every_row_missing(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, seal=False)
    pack = build_adverse_action_pack(collect_capsule_facts(cap))
    rows = _rows(pack)
    assert pack.summary == {"complete": 0, "partial": 0, "missing": 4}
    assert rows["model_call"].reasons == [REASON_NO_MODEL_CALL]
    assert rows["sealed_inputs"].reasons == [REASON_NO_INPUTS]
    assert rows["seal"].reasons == [REASON_NO_SEAL]


def test_other_facets_without_attribution_is_missing(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, extra_manifest={"facets": {"embodied": {"x": 1}}})
    row = _rows(build_adverse_action_pack(collect_capsule_facts(cap)))["principal_reasons"]
    assert (row.status, row.reasons) == ("missing", [REASON_NO_ATTRIBUTION])


def test_facet_without_reasons_is_missing(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet={"method": "shap"})
    row = _rows(build_adverse_action_pack(collect_capsule_facts(cap)))["principal_reasons"]
    assert (row.status, row.reasons) == ("missing", [REASON_NO_REASONS])


def test_unbound_evidence_is_partial(tmp_path: Path) -> None:
    cap = make_capsule(
        tmp_path, calls=[_call("mc-001")], inputs={"a": b"x"}, facet=valid_facet(), bind=False
    )
    rows = _rows(build_adverse_action_pack(collect_capsule_facts(cap)))
    assert rows["model_call"].status == "partial"
    assert rows["model_call"].reasons == [REASON_MODEL_CALLS_UNBOUND]
    assert rows["sealed_inputs"].status == "partial"
    assert "1 of 1 input files not bound" in rows["sealed_inputs"].reasons[0]


def test_unknown_model_call_ref_is_partial(tmp_path: Path) -> None:
    facet = valid_facet() | {"model_call_id": "mc-999"}
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=facet)
    row = _rows(build_adverse_action_pack(collect_capsule_facts(cap)))["principal_reasons"]
    assert row.status == "partial"
    assert "mc-999" in row.reasons[0]


def test_secret_shaped_reason_text_is_suppressed_never_rendered(tmp_path: Path) -> None:
    facet = valid_facet()
    facet["reasons"][0]["description"] = f"token {SECRET}"
    facet["reasons"][2] = {"feature": SECRET}
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=facet)
    pack = build_adverse_action_pack(collect_capsule_facts(cap))
    row = _rows(pack)["principal_reasons"]
    assert row.status == "partial"
    assert "anthropic-api-key" in row.reasons[0]
    assert SECRET not in pack.model_dump_json()
    assert pack.principal_reasons[0].suppressed_fields == ["description"]
    assert pack.principal_reasons[0].feature == "delinquency_count_24m"
    assert pack.principal_reasons[2].suppressed_fields == ["feature"]


def test_integer_contribution_and_absent_rank_are_accepted(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, facet={"reasons": [{"feature": "dti", "contribution": -3}]})
    (reason,) = build_adverse_action_pack(collect_capsule_facts(cap)).principal_reasons
    assert (reason.rank, reason.contribution) == (None, -3)


@pytest.mark.parametrize("field", ["method", "producer", "model_call_id"])
def test_secret_shaped_attribution_field_is_suppressed(tmp_path: Path, field: str) -> None:
    facet = valid_facet() | {field: f"x {SECRET}"}
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=facet)
    pack = build_adverse_action_pack(collect_capsule_facts(cap))
    row = _rows(pack)["principal_reasons"]
    assert row.status == "partial"
    assert any("anthropic-api-key" in r for r in row.reasons)
    assert not any("mc-" in r for r in row.reasons)  # a suppressed id is never echoed
    assert pack.attribution_suppressed_fields == [field]
    assert SECRET not in pack.model_dump_json()


def test_oversize_attribution_field_is_corrupt(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, facet=valid_facet() | {"producer": "p" * 2000})
    with pytest.raises(CorruptCapsuleError, match=r"producer exceeds"):
        collect_capsule_facts(cap)


@pytest.mark.parametrize(
    ("key", "attr"),
    [
        ("model_call_id", "model_call_id"),
        ("gen_ai.system", "system"),
        ("gen_ai.request.model", "request_model"),
        ("gen_ai.response.model", "response_model"),
        ("status", "status"),
        ("started_at", "started_at"),
    ],
)
def test_secret_shaped_model_call_field_is_suppressed(tmp_path: Path, key: str, attr: str) -> None:
    rec = _call("mc-001") | {key: SECRET}
    cap = make_capsule(tmp_path, calls=[rec])
    pack = build_adverse_action_pack(collect_capsule_facts(cap))
    row = _rows(pack)["model_call"]
    assert row.status == "partial"
    assert any("anthropic-api-key" in r for r in row.reasons)
    assert getattr(pack.model_calls[0], attr) is None
    assert pack.model_calls[0].suppressed_fields == [key]
    assert SECRET not in pack.model_dump_json()


def test_absent_model_call_field_is_none_not_suppressed(tmp_path: Path) -> None:
    rec = {k: v for k, v in _call("mc-001").items() if k != "started_at"}
    pack = build_adverse_action_pack(collect_capsule_facts(make_capsule(tmp_path, calls=[rec])))
    assert pack.model_calls[0].started_at is None
    assert pack.model_calls[0].suppressed_fields == []
    assert _rows(pack)["model_call"].status == "complete"


@pytest.mark.parametrize(
    ("value", "match"),
    [({"a": 1}, "must be a scalar"), ("m" * 2000, "exceeds")],
)
def test_malformed_model_call_field_is_corrupt(tmp_path: Path, value: Any, match: str) -> None:
    cap = make_capsule(tmp_path, calls=[_call("mc-001") | {"gen_ai.request.model": value}])
    with pytest.raises(CorruptCapsuleError, match=match):
        collect_capsule_facts(cap)


def test_caps_truncate_to_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect, "MAX_REASONS", 2)
    monkeypatch.setattr(collect, "MAX_MODEL_CALLS", 1)
    monkeypatch.setattr(collect, "MAX_INPUT_FILES", 1)
    cap = make_capsule(
        tmp_path,
        calls=[_call("mc-001"), _call("mc-002")],
        inputs={"a": b"1", "b": b"2"},
        facet=valid_facet(),
    )
    pack = build_adverse_action_pack(collect_capsule_facts(cap))
    rows = _rows(pack)
    assert [r.position for r in pack.principal_reasons] == [1, 2]
    assert rows["principal_reasons"].reasons == ["rendered 2 of 3 recorded reasons (cap reached)"]
    assert rows["model_call"].reasons == ["rendered 1 of 2 recorded model calls (cap reached)"]
    assert rows["sealed_inputs"].reasons == ["rendered 1 of 2 input files (cap reached)"]


def test_no_notice_verdict_or_outcome_field(tmp_path: Path) -> None:
    payload = json.loads(
        build_adverse_action_pack(collect_capsule_facts(_full(tmp_path))).model_dump_json()
    )
    keys: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                keys.add(k.lower())
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(payload)
    for forbidden in ("notice", "verdict", "compliant", "outcome", "approved", "denied", "score"):
        assert not any(forbidden in k for k in keys), forbidden


# --------------------------------------------------------------------------- corrupt evidence


def test_forged_model_call_digest_is_corrupt(tmp_path: Path) -> None:
    cap = _full(tmp_path)
    (cap / "model-calls.jsonl").write_text(json.dumps(_call("mc-001", "other-model")) + "\n")
    with pytest.raises(CorruptCapsuleError, match="does not match its sealed"):
        collect_capsule_facts(cap)


def test_forged_input_digest_is_corrupt(tmp_path: Path) -> None:
    cap = _full(tmp_path)
    (cap / "inputs" / "application.json").write_bytes(b"tampered")
    with pytest.raises(CorruptCapsuleError, match="inputs/application.json"):
        collect_capsule_facts(cap)


@pytest.mark.parametrize(
    ("content", "match"),
    [(b"{not json\n", "not JSON"), (b"[1, 2]\n", "not a JSON object"), (b"\xff\xfe\n", "not JSON")],
)
def test_malformed_model_call_line_is_corrupt(tmp_path: Path, content: bytes, match: str) -> None:
    cap = make_capsule(tmp_path, bind=False)
    (cap / "model-calls.jsonl").write_bytes(b"\n" + content)
    with pytest.raises(CorruptCapsuleError, match=match):
        collect_capsule_facts(cap)


@pytest.mark.parametrize(
    ("manifest", "match"),
    [
        (b"- a\n- b\n", "not a mapping"),
        (b"run_id: [unclosed\n", "unparseable"),
        (b"\xff\xfe", "unparseable"),
        (b"run_id: r\nevidence_digests: [1]\n", "evidence_digests is not a mapping"),
        (b"run_id: r\nevidence_digests:\n  x: {size_bytes: 1}\n", "carries no sha256"),
        (b"run_id: r\nfacets: [1]\n", "facets is not a mapping"),
        (b"run_id: r\nfacets:\n  feature_attribution: [1]\n", "is not a mapping"),
        (b"run_id: r\nfacets:\n  feature_attribution:\n    reasons: x\n", "reasons is not a list"),
    ],
)
def test_malformed_manifest_is_corrupt(tmp_path: Path, manifest: bytes, match: str) -> None:
    cap = tmp_path / "cap"
    cap.mkdir()
    (cap / "capsule.yaml").write_bytes(manifest)
    with pytest.raises(CorruptCapsuleError, match=match):
        collect_capsule_facts(cap)


def test_invalid_golden_fixture_is_corrupt(tmp_path: Path) -> None:
    facet = yaml.safe_load((FIXTURES / "feature_attribution_invalid.yaml").read_text())
    cap = make_capsule(tmp_path, calls=[_call("mc-001")], facet=facet)
    with pytest.raises(CorruptCapsuleError, match=r"reasons\[1\] is not a mapping"):
        collect_capsule_facts(cap)


@pytest.mark.parametrize(
    ("reason", "match"),
    [
        ({"rank": 1}, "names no feature"),
        ({"feature": "x", "rank": [1]}, "rank must be an integer"),
        ({"feature": "x", "rank": SECRET}, "rank must be an integer"),
        ({"feature": "x", "rank": "1"}, "rank must be an integer"),
        ({"feature": "x", "rank": True}, "rank must be an integer"),
        ({"feature": "x", "rank": 1.5}, "rank must be an integer"),
        ({"feature": "x", "contribution": {"v": 1}}, "contribution must be a number"),
        ({"feature": "x", "contribution": SECRET}, "contribution must be a number"),
        ({"feature": "x", "contribution": "-0.31"}, "contribution must be a number"),
        ({"feature": "x", "contribution": False}, "contribution must be a number"),
        ({"feature": "x", "contribution": float("nan")}, "contribution must be finite"),
        ({"feature": "x", "contribution": float("inf")}, "contribution must be finite"),
        ({"feature": 7}, "feature must be a string"),
        ({"description": "x" * 2000}, "exceeds"),
    ],
)
def test_malformed_reason_is_corrupt(tmp_path: Path, reason: dict[str, Any], match: str) -> None:
    cap = make_capsule(tmp_path, facet={"reasons": [reason]})
    with pytest.raises(CorruptCapsuleError, match=match):
        collect_capsule_facts(cap)


def test_missing_manifest_is_corrupt(tmp_path: Path) -> None:
    with pytest.raises(CorruptCapsuleError, match="no capsule.yaml"):
        collect_capsule_facts(tmp_path)


def test_oversize_manifest_is_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _full(tmp_path)
    monkeypatch.setattr(collect, "MANIFEST_MAX_BYTES", 10)
    with pytest.raises(CorruptCapsuleError, match="limit 10"):
        collect_capsule_facts(cap)


def test_unreadable_model_calls_is_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _full(tmp_path)
    real = os.open

    def boom(path: Any, flags: int, *args: Any) -> int:
        if Path(path).name == "model-calls.jsonl":
            raise PermissionError("denied")
        return real(path, flags, *args)

    monkeypatch.setattr(_sealed_read.os, "open", boom)
    with pytest.raises(CorruptCapsuleError, match="cannot read model-calls stream"):
        collect_capsule_facts(cap)


def test_unreadable_input_is_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = _full(tmp_path)

    def boom(path: Path) -> tuple[str, int]:
        raise PermissionError("denied")

    monkeypatch.setattr(collect, "_sha256_file", boom)
    with pytest.raises(CorruptCapsuleError, match="cannot read input file"):
        collect_capsule_facts(cap)


# --------------------------------------------------------------------------- read-only / symlinks


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_collector_is_read_only(tmp_path: Path) -> None:
    cap = _full(tmp_path)
    before = _snapshot(tmp_path)
    collect_capsule_facts(cap)
    assert _snapshot(tmp_path) == before


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("host secret")
    (outside / "calls.jsonl").write_text(json.dumps(_call("mc-x")) + "\n")
    cap = make_capsule(tmp_path, facet=valid_facet(), seal=False, bind=False)
    (cap / "inputs" / "link.txt").symlink_to(outside / "secret.txt")
    (cap / "inputs" / "linkdir").symlink_to(outside, target_is_directory=True)
    (cap / ".seal").mkdir()
    (cap / ".seal" / "manifest.dsse").symlink_to(outside / "secret.txt")
    facts = collect_capsule_facts(cap)
    assert facts.inputs == [] and facts.model_calls == [] and facts.seal_ref is None


# ----------------------------------------------------------- sealed evidence that vanished (exit 2)


def test_symlinked_model_calls_is_corrupt_even_unsealed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "calls.jsonl").write_text(json.dumps(_call("mc-x")) + "\n")
    cap = make_capsule(tmp_path, facet=valid_facet(), bind=False)
    (cap / "model-calls.jsonl").symlink_to(outside / "calls.jsonl")
    with pytest.raises(CorruptCapsuleError, match="symlink"):
        collect_capsule_facts(cap)


def test_symlinked_manifest_is_corrupt(tmp_path: Path) -> None:
    cap = _full(tmp_path)
    real = tmp_path / "real.yaml"
    real.write_bytes((cap / "capsule.yaml").read_bytes())
    (cap / "capsule.yaml").unlink()
    (cap / "capsule.yaml").symlink_to(real)
    with pytest.raises(CorruptCapsuleError, match="symlink"):
        collect_capsule_facts(cap)


def _replace_with_symlink(path: Path, outside: Path) -> None:
    outside.write_bytes(path.read_bytes())  # identical bytes: the digest would still match
    path.unlink()
    path.symlink_to(outside)


def _replace_with_fifo(path: Path, outside: Path) -> None:
    path.unlink()
    os.mkfifo(path)


def _replace_with_dir(path: Path, outside: Path) -> None:
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda p, o: p.unlink(), "sealed in evidence_digests but absent"),
        (_replace_with_symlink, "symlink"),
        (_replace_with_fifo, "not a regular file"),
        (_replace_with_dir, "not a regular file"),
    ],
    ids=["deleted", "symlink", "fifo", "directory"],
)
def test_sealed_model_calls_that_vanished_is_corrupt(
    tmp_path: Path, mutate: Any, match: str
) -> None:
    cap = _full(tmp_path)
    mutate(cap / "model-calls.jsonl", tmp_path / "outside.jsonl")
    with pytest.raises(CorruptCapsuleError, match=match):
        collect_capsule_facts(cap)


@pytest.mark.parametrize(
    "mutate", [lambda p, o: p.unlink(), _replace_with_symlink, _replace_with_fifo]
)
def test_sealed_input_that_vanished_is_corrupt(tmp_path: Path, mutate: Any) -> None:
    cap = _full(tmp_path)
    mutate(cap / "inputs" / "application.json", tmp_path / "outside.json")
    with pytest.raises(CorruptCapsuleError, match="inputs/application.json is sealed"):
        collect_capsule_facts(cap)


def test_sealed_input_with_inputs_dir_gone_is_corrupt(tmp_path: Path) -> None:
    cap = _full(tmp_path)
    (cap / "inputs" / "application.json").unlink()
    (cap / "inputs").rmdir()
    with pytest.raises(CorruptCapsuleError, match="inputs/application.json is sealed"):
        collect_capsule_facts(cap)


def test_sealed_input_beyond_the_render_cap_is_still_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collect, "MAX_INPUT_FILES", 1)
    cap = make_capsule(tmp_path, inputs={"a.json": b"a", "b.json": b"b"})
    facts = collect_capsule_facts(cap)
    assert facts.total_inputs == 2 and len(facts.inputs) == 1


def test_oversize_model_calls_is_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collect, "MODEL_CALLS_MAX_BYTES", 10)
    cap = _full(tmp_path)
    with pytest.raises(CorruptCapsuleError, match="exceeds its read bound"):
        collect_capsule_facts(cap)


def test_unsealed_fifo_or_absent_model_calls_stays_absent(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, facet=valid_facet(), bind=False)
    assert collect_capsule_facts(cap).model_calls == []
    os.mkfifo(cap / "model-calls.jsonl")
    facts = collect_capsule_facts(cap)
    assert facts.model_calls == [] and facts.total_model_calls == 0
