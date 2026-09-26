"""ADR-0159 D6 / NF-280 — CAT-style lifecycle agent-event trail and its collector.

Acceptance criteria covered:
- every recorded event renders in lifecycle order (UTC-normalised timestamp, then stage order),
  each with its lifecycle stage, recorded actor identity ref, and recorded timestamps;
- the explicit stage mapping: capsule created_at -> origination, model calls -> decision,
  tool-permission decisions + human approvals -> authorization, tool calls -> action,
  capsule finished_at -> disposition; no event is ever synthesised or dropped;
- a missing required lifecycle stage -> trail ``partial`` (spec §4.1); no events -> ``missing``;
  a missing ``authorization`` stage alone does not downgrade the trail;
- truncation / unbound digests / suppressed secret-shaped text / untimed events -> ``partial``;
- prompt/response text, tool arguments/results, and approval rationales never reach the trail;
- forged digests, malformed lines, symlinked evidence, over-long fields, and unparseable or
  offset-less timestamps raise ``CorruptCapsuleError``;
- the trail carries the version-pinned regime, the CAT honesty line and the finance banner, and
  has no submission / transmitted / reporter field; the collector opens no network socket.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

import pytest
import yaml
from _cat_capsule import SECRET, STREAMS, invalid_tool_calls, make_capsule, sha, valid_fixture

from novafabric.compliance.export.finance import _sealed_read, cat_collect
from novafabric.compliance.export.finance.cat_collect import (
    CorruptCapsuleError,
    collect_trail_facts,
)
from novafabric.compliance.export.finance.cat_trail import (
    CAT_HONESTY_LINE,
    CAT_REGIME,
    FINANCE_HONESTY_BANNER,
    REASON_TRAIL_EMPTY,
    REQUIRED_STAGES,
    build_cat_trail,
)
from novafabric.compliance.export.provenance import EvidenceSource


def _trail(cap: Path) -> Any:
    return build_cat_trail(collect_trail_facts(cap))


def _rows(trail: Any) -> dict[str, Any]:
    return {r.key: r for r in trail.rows}


# --------------------------------------------------------------------------- happy path


def test_full_capsule_renders_every_stage_complete_in_lifecycle_order(tmp_path: Path) -> None:
    trail = _trail(make_capsule(tmp_path))
    assert trail.trail_status == "complete"
    assert trail.trail_reasons == []
    assert trail.summary == {"complete": 5, "partial": 0, "missing": 0}
    got = [(e.sequence, e.stage, e.event_type) for e in trail.events]
    assert got == [
        (1, "origination", "run_started"),
        (2, "decision", "model_call"),
        (3, "authorization", "tool_permission_decision"),
        (4, "authorization", "human_approval"),
        (5, "action", "tool_call"),
        (6, "disposition", "run_finished"),
    ]
    assert trail.regime == CAT_REGIME
    assert "17 CFR 242.613" in trail.regime and "34-79318" in trail.regime
    assert trail.cat_honesty == CAT_HONESTY_LINE
    assert trail.banner == FINANCE_HONESTY_BANNER
    assert trail.run_id == "01RUNCAT"
    assert trail.run_status == "success"
    assert trail.seal_ref == ".seal/manifest.dsse"
    assert trail.required_stages == list(REQUIRED_STAGES)
    assert set(trail.stage_mapping) == {r.key for r in trail.rows}


def test_actor_identity_refs_and_timestamps_are_recorded_values(tmp_path: Path) -> None:
    trail = _trail(make_capsule(tmp_path))
    by_type = {e.event_type: e for e in trail.events}
    mc = by_type["model_call"]
    assert (mc.actor_kind, mc.actor_ref, mc.actor_detail) == (
        "model",
        "order-router-v2-2026-08",
        "openai",
    )
    assert (mc.event_id, mc.timestamp, mc.ended_at) == (
        "01MCDECIDE",
        "2026-09-01T10:00:01.250000Z",
        "2026-09-01T10:00:02Z",
    )
    perm = by_type["tool_permission_decision"]
    assert perm.actor_ref == "spiffe://firm.example/agent/router"
    assert perm.approver_ref == "trader:jdoe"
    assert perm.outcome == "escalated_to_human"
    assert perm.subject_ref == "submit_order"
    assert perm.timestamp == "2026-09-01T12:00:03+02:00"  # verbatim
    assert perm.timestamp_utc == "2026-09-01T10:00:03.000000Z"  # normalised for ordering
    appr = by_type["human_approval"]
    assert (appr.actor_kind, appr.actor_ref, appr.outcome) == (
        "human-approver",
        "trader:jdoe",
        "approved",
    )
    tc = by_type["tool_call"]
    assert (tc.actor_ref, tc.caused_by_ref, tc.actor_detail) == (
        "submit_order",
        "01MCDECIDE",
        "internal-oms",
    )
    origin = by_type["run_started"]
    assert origin.actor_ref is None  # nothing recorded at run level — never inferred
    assert by_type["run_finished"].outcome == "success"


def test_record_digest_binds_the_exact_line(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path)
    trail = _trail(cap)
    line = (cap / "tool-calls.jsonl").read_bytes().split(b"\n")[0]
    (tc,) = [e for e in trail.events if e.event_type == "tool_call"]
    assert tc.record_sha256 == sha(line)
    assert tc.source_ref == "tool-calls.jsonl#L1"
    assert _rows(trail)["action"].source_refs == [f"{cap}/tool-calls.jsonl#L1"]


def test_no_prompt_arguments_or_rationale_text_reaches_the_trail(tmp_path: Path) -> None:
    dumped = _trail(make_capsule(tmp_path)).model_dump_json()
    for needle in ("ACCT-0042", "route to venue A", "OMS-777", "limit ok"):
        assert needle not in dumped


def test_no_submission_or_transmission_field(tmp_path: Path) -> None:
    data = _trail(make_capsule(tmp_path)).model_dump()
    keys = set(data)
    for forbidden in ("transmitted", "submitted", "cat_reporter_id", "compliant", "verdict"):
        assert forbidden not in keys


def test_yaml_datetime_timestamps_are_accepted(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path)
    body = yaml.safe_load((cap / "capsule.yaml").read_text())
    text = yaml.safe_dump({k: v for k, v in body.items() if k not in {"created_at"}})
    (cap / "capsule.yaml").write_text(text + "created_at: 2026-09-01T09:59:59Z\n")
    trail = _trail(cap)
    assert trail.events[0].event_type == "run_started"
    assert trail.events[0].timestamp_utc == "2026-09-01T09:59:59.000000Z"


def test_ordering_ties_break_on_stage_then_stream_then_line(tmp_path: Path) -> None:
    ts = "2026-09-01T10:00:00Z"
    fx = valid_fixture()
    tc = dict(fx["tool-calls.jsonl"][0], started_at=ts)
    mc = dict(fx["model-calls.jsonl"][0], started_at=ts)
    cap = make_capsule(
        tmp_path,
        streams={
            "tool-calls.jsonl": [tc, dict(tc, tool_call_id="second")],
            "model-calls.jsonl": [mc],
        },
        manifest={"created_at": ts, "finished_at": ts, "status": "success"},
    )
    got = [(e.stage, e.event_id) for e in _trail(cap).events]
    assert got == [
        ("origination", None),
        ("decision", "01MCDECIDE"),
        ("action", "01TCSUBMIT"),
        ("action", "second"),
        ("disposition", None),
    ]


# --------------------------------------------------------------------------- missing / partial


def test_missing_required_stage_makes_trail_partial(tmp_path: Path) -> None:
    fx = valid_fixture()
    cap = make_capsule(tmp_path, streams={"model-calls.jsonl": fx["model-calls.jsonl"]})
    trail = _trail(cap)
    rows = _rows(trail)
    assert rows["action"].status == "missing"
    assert rows["action"].source_refs == []
    assert rows["action"].evidence_source is EvidenceSource.unverifiable
    assert "tool-calls.jsonl" in rows["action"].reasons[0]
    assert rows["authorization"].status == "missing"
    assert rows["authorization"].required is False
    assert trail.trail_status == "partial"
    assert trail.trail_reasons == ["required lifecycle stage(s) not recorded: action"]


def test_missing_optional_authorization_alone_keeps_trail_complete(tmp_path: Path) -> None:
    fx = valid_fixture()
    streams = {k: fx[k] for k in ("model-calls.jsonl", "tool-calls.jsonl")}
    trail = _trail(make_capsule(tmp_path, streams=streams))
    assert _rows(trail)["authorization"].status == "missing"
    assert trail.trail_status == "complete"


def test_no_event_at_all_is_missing(tmp_path: Path) -> None:
    trail = _trail(make_capsule(tmp_path, streams={}, manifest={}))
    assert trail.events == []
    assert trail.trail_status == "missing"
    assert trail.trail_reasons == [REASON_TRAIL_EMPTY]
    assert trail.summary == {"complete": 0, "partial": 0, "missing": 5}
    assert trail.run_status is None


def test_unfinished_run_has_no_disposition_event(tmp_path: Path) -> None:
    manifest = {"created_at": "2026-09-01T10:00:00Z", "status": "running"}
    trail = _trail(make_capsule(tmp_path, manifest=manifest))
    assert _rows(trail)["disposition"].status == "missing"
    assert "disposition" in trail.trail_reasons[0]


def test_untimed_event_is_kept_last_and_partial(tmp_path: Path) -> None:
    fx = valid_fixture()
    tc = {k: v for k, v in fx["tool-calls.jsonl"][0].items() if k != "started_at"}
    streams = {"model-calls.jsonl": fx["model-calls.jsonl"], "tool-calls.jsonl": [tc]}
    trail = _trail(make_capsule(tmp_path, streams=streams))
    assert trail.events[-1].event_type == "tool_call"
    assert trail.events[-1].timestamp is None
    row = _rows(trail)["action"]
    assert row.status == "partial"
    assert "no recorded timestamp" in row.reasons[0]
    assert trail.trail_status == "partial"


def test_unbound_streams_are_partial(tmp_path: Path) -> None:
    trail = _trail(make_capsule(tmp_path, bind=False))
    assert _rows(trail)["decision"].status == "partial"
    assert "not bound" in _rows(trail)["decision"].reasons[0]
    assert _rows(trail)["origination"].status == "complete"  # manifest fields are DSSE-bound
    assert trail.seal_ref == ".seal/manifest.dsse"


def test_truncation_is_reported_never_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cat_collect, "MAX_EVENTS_PER_STREAM", 1)
    fx = valid_fixture()
    tc = fx["tool-calls.jsonl"][0]
    cap = make_capsule(tmp_path, streams={"tool-calls.jsonl": [tc, tc, tc]})
    trail = _trail(cap)
    row = _rows(trail)["action"]
    assert row.status == "partial"
    assert row.event_count == 1
    assert "rendered 1 of 3 recorded tool-calls.jsonl events" in row.reasons[0]
    (stream,) = [s for s in trail.streams if s.name == "tool-calls.jsonl"]
    assert (stream.total, stream.rendered) == (3, 1)


def test_secret_shaped_values_are_suppressed_never_rendered(tmp_path: Path) -> None:
    fx = valid_fixture()
    perm = dict(fx["tool-permission-events.jsonl"][0], authorising_identity=SECRET)
    tc = dict(fx["tool-calls.jsonl"][0], tool_provider=SECRET)
    cap = make_capsule(
        tmp_path,
        streams={"tool-permission-events.jsonl": [perm], "tool-calls.jsonl": [tc]},
        manifest={"created_at": "2026-09-01T10:00:00Z", "status": SECRET},
    )
    trail = _trail(cap)
    assert SECRET not in trail.model_dump_json()
    by_type = {e.event_type: e for e in trail.events}
    assert by_type["tool_permission_decision"].actor_ref is None
    assert by_type["tool_permission_decision"].suppressed_fields == ["authorising_identity"]
    assert by_type["tool_call"].suppressed_fields == ["tool_provider"]
    rows = _rows(trail)
    assert rows["authorization"].status == "partial"
    assert "suppressed" in rows["authorization"].reasons[0]
    assert rows["origination"].status == "partial"  # manifest status suppressed
    assert trail.run_status is None


def test_secret_shaped_run_id_falls_back_to_capsule_dir_name(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path)
    body = yaml.safe_load((cap / "capsule.yaml").read_text())
    body["run_id"] = SECRET
    (cap / "capsule.yaml").write_text(yaml.safe_dump(body))
    trail = _trail(cap)
    assert trail.run_id == "01RUNCAT"
    assert SECRET not in trail.model_dump_json()


# --------------------------------------------------------------------------- corrupt evidence


@pytest.mark.parametrize("case", sorted(invalid_tool_calls()))
def test_invalid_golden_records_are_corrupt(tmp_path: Path, case: str) -> None:
    record = invalid_tool_calls()[case]
    cap = make_capsule(tmp_path, streams={"tool-calls.jsonl": [record]})
    with pytest.raises(CorruptCapsuleError):
        collect_trail_facts(cap)


def test_forged_stream_digest_is_corrupt(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path)
    with (cap / "tool-calls.jsonl").open("a") as fh:
        fh.write('{"tool_call_id":"injected"}\n')
    with pytest.raises(CorruptCapsuleError, match="does not match"):
        collect_trail_facts(cap)


@pytest.mark.parametrize("line", [b"not json\n", b"[1, 2]\n"])
def test_malformed_stream_line_is_corrupt(tmp_path: Path, line: bytes) -> None:
    cap = make_capsule(tmp_path, streams={}, bind=False)
    (cap / "model-calls.jsonl").write_bytes(line)
    with pytest.raises(CorruptCapsuleError, match="line 1"):
        collect_trail_facts(cap)


def test_symlinked_stream_is_corrupt_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(valid_fixture()["tool-calls.jsonl"][0]) + "\n")
    cap = make_capsule(tmp_path / "c", streams={}, bind=False)
    (cap / "tool-calls.jsonl").symlink_to(outside)
    with pytest.raises(CorruptCapsuleError, match="symlink"):
        collect_trail_facts(cap)


def test_symlinked_manifest_is_corrupt(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path / "c")
    real = tmp_path / "real.yaml"
    real.write_bytes((cap / "capsule.yaml").read_bytes())
    (cap / "capsule.yaml").unlink()
    (cap / "capsule.yaml").symlink_to(real)
    with pytest.raises(CorruptCapsuleError, match="symlink"):
        collect_trail_facts(cap)


def test_unsealed_stream_directory_or_fifo_is_treated_as_absent(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path, streams={}, bind=False)
    (cap / "tool-calls.jsonl").mkdir()
    os.mkfifo(cap / "model-calls.jsonl")
    rows = _rows(_trail(cap))
    assert rows["action"].status == "missing" and rows["decision"].status == "missing"


def _to_symlink(path: Path, outside: Path) -> None:
    outside.write_bytes(path.read_bytes())  # identical bytes: the digest would still match
    path.unlink()
    path.symlink_to(outside)


def _to_fifo(path: Path, outside: Path) -> None:
    path.unlink()
    os.mkfifo(path)


def _to_dir(path: Path, outside: Path) -> None:
    path.unlink()
    path.mkdir()


@pytest.mark.parametrize("stream", STREAMS)
@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda p, o: p.unlink(), "sealed in evidence_digests but absent"),
        (_to_symlink, "symlink"),
        (_to_fifo, "not a regular file"),
        (_to_dir, "not a regular file"),
    ],
    ids=["deleted", "symlink", "fifo", "directory"],
)
def test_sealed_stream_that_vanished_is_corrupt_never_missing(
    tmp_path: Path, stream: str, mutate: Any, match: str
) -> None:
    """Reviewer PoC: unlinking a sealed stream must not yield an all-``complete`` trail."""
    cap = make_capsule(tmp_path)
    mutate(cap / stream, tmp_path / "outside.jsonl")
    with pytest.raises(CorruptCapsuleError, match=match):
        collect_trail_facts(cap)


def test_stream_swapped_between_lstat_and_open_is_corrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = make_capsule(tmp_path)
    target = cap / "tool-calls.jsonl"
    real_open = os.open

    def swap(path: Any, flags: int, *args: Any) -> int:
        if Path(path) == target:
            staged = target.with_suffix(".swap")
            staged.write_bytes(target.read_bytes())  # same bytes, new inode
            os.replace(staged, target)
        return real_open(path, flags, *args)

    monkeypatch.setattr(_sealed_read.os, "open", swap)
    with pytest.raises(CorruptCapsuleError, match="changed while being opened"):
        collect_trail_facts(cap)


def test_unopenable_stream_is_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = make_capsule(tmp_path)
    real_open = os.open

    def boom(path: Any, flags: int, *args: Any) -> int:
        if Path(path).name == "tool-calls.jsonl":
            raise PermissionError("denied")
        return real_open(path, flags, *args)

    monkeypatch.setattr(_sealed_read.os, "open", boom)
    with pytest.raises(CorruptCapsuleError, match="cannot read event stream"):
        collect_trail_facts(cap)


def test_unstatable_stream_is_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cap = make_capsule(tmp_path)
    real_lstat = Path.lstat

    def boom(self: Path) -> os.stat_result:
        if self.name == "tool-calls.jsonl":
            raise PermissionError("denied")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", boom)
    with pytest.raises(CorruptCapsuleError, match="cannot stat event stream"):
        collect_trail_facts(cap)


def test_oversize_stream_is_corrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cat_collect, "STREAM_MAX_BYTES", 10)
    cap = make_capsule(tmp_path)
    with pytest.raises(CorruptCapsuleError, match="exceeds"):
        collect_trail_facts(cap)


@pytest.mark.parametrize(
    ("text", "match"),
    [
        (None, "no capsule.yaml"),
        ("a: [unclosed\n", "unparseable"),
        ("- a\n", "not a mapping"),
        ("evidence_digests: [1]\n", "not a mapping"),
        ("evidence_digests: {tool-calls.jsonl: 3}\n", "no sha256"),
        ("created_at: 2026-09-01T10:00:00\n", "no UTC offset"),
        (f"created_at: '{'2' * 80}'\n", "exceeds"),
    ],
)
def test_malformed_manifest_is_corrupt(tmp_path: Path, text: str | None, match: str) -> None:
    cap = tmp_path / "01RUNCAT"
    cap.mkdir()
    if text is not None:
        (cap / "capsule.yaml").write_text(text)
    with pytest.raises(CorruptCapsuleError, match=match):
        collect_trail_facts(cap)


# --------------------------------------------------------------------------- read-only / offline


def test_collector_is_read_only_and_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = make_capsule(tmp_path)
    before = {p: p.read_bytes() for p in cap.rglob("*") if p.is_file()}

    def _no_network(*_a: object, **_k: object) -> None:
        raise AssertionError("NF-280 must never open a network connection")

    monkeypatch.setattr(socket, "socket", _no_network)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    _trail(cap)
    after = {p: p.read_bytes() for p in cap.rglob("*") if p.is_file()}
    assert before == after


def test_output_is_deterministic(tmp_path: Path) -> None:
    cap = make_capsule(tmp_path)
    assert _trail(cap).model_dump_json() == _trail(cap).model_dump_json()


def test_actor_falls_back_to_request_model_and_absent_actor_is_none(tmp_path: Path) -> None:
    fx = valid_fixture()
    mc = {k: v for k, v in fx["model-calls.jsonl"][0].items() if k != "gen_ai.response.model"}
    bare = {"model_call_id": "bare", "started_at": "2026-09-01T10:00:02Z"}
    trail = _trail(make_capsule(tmp_path, streams={"model-calls.jsonl": [mc, bare]}))
    refs = [e.actor_ref for e in trail.events if e.stage == "decision"]
    assert refs == ["order-router-v2", None]
