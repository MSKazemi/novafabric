"""ADR-0123 P5: sub-range replay, per-turn mode policy (D6), and dry-run plan.

Acceptance criteria under test:

- ``from_seq``/``to_seq`` replay exactly the inclusive slice, in order, and
  record it as ``range``; a single bound extends to the session edge;
- invalid bounds (outside the session, from > to) and pins naming a turn that
  is absent or outside the slice are refused with a named error — before any
  turn executes;
- ``turn_modes`` pins set each pinned turn's ``effective_mode`` and are logged
  as ``turn_mode_policy``; unpinned turns use the session mode;
- the emitted record validates against the (additively extended) schema;
- ``plan_session_replay`` executes nothing and writes nothing, reports the
  same selection, per-turn integrity, and tool exposure under the inherited
  per-capsule policy.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from novafabric.session import (
    SessionReplayRangeError,
    add_member,
    new_session,
    plan_session_replay,
    replay_session,
    save_session,
)

REPO_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "session-replay-result.schema.json"

RUNS = [
    "01HZ8T0A00YZ2K7N9DPBYK2W01",
    "01HZ8T1B00YZ2K7N9DPBYK2W02",
    "01HZ8T2C00YZ2K7N9DPBYK2W03",
    "01HZ8T3D00YZ2K7N9DPBYK2W04",
]
PASS_CMD = [sys.executable, "-c", "pass"]


def make_capsule(base: Path, run_id: str, tool_calls: list[dict[str, str]] | None = None) -> Path:
    capsule_dir = base / run_id
    capsule_dir.mkdir(parents=True)
    (capsule_dir / "capsule.yaml").write_text(
        yaml.dump(
            {
                "schema_version": "1.0.0",
                "run_id": run_id,
                "created_at": "2026-07-15T09:00:00.000000Z",
                "status": "success",
                "command": PASS_CMD,
            }
        )
    )
    (capsule_dir / "model-calls.jsonl").write_text("")
    if tool_calls is not None:
        (capsule_dir / "tool-calls.jsonl").write_text(
            "".join(json.dumps(tc) + "\n" for tc in tool_calls)
        )
    return capsule_dir


def build(
    tmp_path: Path, n: int = 4, tools: dict[int, list[dict[str, str]]] | None = None
) -> tuple[str, Path, Path, Path]:
    root, caps, replays = tmp_path / "sessions", tmp_path / "caps", tmp_path / "replays"
    manifest = new_session()
    save_session(manifest, root=root)
    for i, run_id in enumerate(RUNS[:n]):
        add_member(manifest, make_capsule(caps, run_id, (tools or {}).get(i)), root=root)
    save_session(manifest, root=root)
    return manifest.session_id, root, caps, replays


def _validate(record: dict[str, object]) -> None:
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(
        record
    )


class TestSubRange:
    def test_inclusive_slice_is_replayed_and_recorded(self, tmp_path: Path) -> None:
        sid, root, caps, replays = build(tmp_path)
        result = replay_session(
            sid, root=root, capsule_base=caps, base_dir=replays, from_seq=1, to_seq=2
        )
        assert [t.sequence for t in result.turns] == [1, 2]
        assert result.range == (1, 2)
        assert result.whole_session_verdict == "reproduced"
        record = result.to_json_dict()
        assert record["range"] == {"from": 1, "to": 2}
        assert "turn_mode_policy" not in record
        _validate(record)

    @pytest.mark.parametrize(
        ("from_seq", "to_seq", "expected"),
        [(2, None, [2, 3]), (None, 1, [0, 1]), (3, 3, [3])],
    )
    def test_single_bound_extends_to_edge(
        self, tmp_path: Path, from_seq: int | None, to_seq: int | None, expected: list[int]
    ) -> None:
        sid, root, caps, replays = build(tmp_path)
        result = replay_session(
            sid,
            mode="forensic",
            root=root,
            capsule_base=caps,
            base_dir=replays,
            from_seq=from_seq,
            to_seq=to_seq,
        )
        assert [t.sequence for t in result.turns] == expected
        assert result.range == (expected[0], expected[-1])

    def test_no_bounds_means_no_range(self, tmp_path: Path) -> None:
        sid, root, caps, replays = build(tmp_path, n=2)
        result = replay_session(
            sid, mode="forensic", root=root, capsule_base=caps, base_dir=replays
        )
        assert result.range is None
        assert "range" not in result.to_json_dict()

    @pytest.mark.parametrize(
        ("from_seq", "to_seq", "match"),
        [(5, None, "--from 5 is outside"), (None, 9, "--to 9 is outside"), (3, 1, "after")],
    )
    def test_invalid_bounds_refused_before_execution(
        self, tmp_path: Path, from_seq: int | None, to_seq: int | None, match: str
    ) -> None:
        sid, root, caps, replays = build(tmp_path)
        with pytest.raises(SessionReplayRangeError, match=match):
            replay_session(
                sid,
                root=root,
                capsule_base=caps,
                base_dir=replays,
                from_seq=from_seq,
                to_seq=to_seq,
            )
        assert not replays.exists()  # nothing executed

    def test_halt_inside_slice_leaves_later_turns_absent(self, tmp_path: Path) -> None:
        import shutil

        sid, root, caps, replays = build(tmp_path)
        shutil.rmtree(caps / RUNS[1])
        result = replay_session(
            sid, root=root, capsule_base=caps, base_dir=replays, from_seq=1, to_seq=3
        )
        assert [t.status for t in result.turns] == ["refused"]
        assert result.whole_session_verdict == "refused"


class TestTurnModePolicy:
    def test_pins_set_effective_mode_and_are_logged(self, tmp_path: Path) -> None:
        sid, root, caps, replays = build(tmp_path, n=3)
        result = replay_session(
            sid,
            root=root,
            capsule_base=caps,
            base_dir=replays,
            turn_modes={2: "forensic", 0: "forensic"},
        )
        assert [t.effective_mode for t in result.turns] == ["forensic", "mocked", "forensic"]
        assert result.mode == "mocked"
        record = result.to_json_dict()
        assert record["turn_mode_policy"] == {"0": "forensic", "2": "forensic"}
        assert list(record["turn_mode_policy"]) == ["0", "2"]  # sorted, deterministic
        _validate(record)

    def test_pin_for_absent_turn_refused(self, tmp_path: Path) -> None:
        sid, root, caps, replays = build(tmp_path, n=2)
        with pytest.raises(SessionReplayRangeError, match="no such turn"):
            replay_session(
                sid, root=root, capsule_base=caps, base_dir=replays, turn_modes={7: "exact"}
            )

    def test_pin_outside_slice_refused_not_ignored(self, tmp_path: Path) -> None:
        sid, root, caps, replays = build(tmp_path)
        with pytest.raises(SessionReplayRangeError, match="would never apply"):
            replay_session(
                sid,
                root=root,
                capsule_base=caps,
                base_dir=replays,
                from_seq=0,
                to_seq=1,
                turn_modes={3: "forensic"},
            )

    def test_schema_rejects_bad_policy(self) -> None:
        base = json.loads(
            (REPO_ROOT / "tests/fixtures/session-replay/valid-mocked-reproduced.json").read_text()
        )
        for bad in ({"01": "mocked"}, {"1": "turbo"}, {}):
            record = {**base, "turn_mode_policy": bad}
            with pytest.raises(jsonschema.ValidationError):
                _validate(record)


class TestDryRunPlan:
    TOOLS = {
        0: [
            {"tool_call_id": "t1", "tool_name": "search", "mutation_class": "read-only"},
            {
                "tool_call_id": "t2",
                "tool_name": "write_db",
                "mutation_class": "non-idempotent-write",
            },
        ],
        1: [{"tool_call_id": "t3", "tool_name": "email", "mutation_class": "external-side-effect"}],
    }

    def test_plan_executes_nothing_and_reports_exposure(self, tmp_path: Path) -> None:
        sid, root, caps, replays = build(tmp_path, n=3, tools=self.TOOLS)
        plan = plan_session_replay(
            sid, mode="semantic", root=root, capsule_base=caps, turn_modes={2: "forensic"}
        )
        assert not replays.exists()
        assert plan.total_turns == 3
        assert plan.range is None
        assert [t.effective_mode for t in plan.turns] == ["semantic", "semantic", "forensic"]
        assert [t.mode_pinned for t in plan.turns] == [False, False, True]
        assert all(t.integrity == "ok" and not t.would_refuse for t in plan.turns)

        turn0 = plan.turns[0].tool_exposure
        assert turn0 is not None
        assert (turn0.tool_calls, turn0.mutating) == (2, 1)
        # semantic under default flags: read-only is denied too (ladder starts at none)
        assert turn0.decisions == {"deny": 2}
        assert turn0.mutation_classes == {"read-only": 1, "non-idempotent-write": 1}
        turn2 = plan.turns[2].tool_exposure
        assert turn2 is not None and turn2.tool_calls == 0

        data = plan.to_json_dict()
        assert data["turn_mode_policy"] == {"2": "forensic"}
        assert data["range"] is None
        json.dumps(data)  # serializable

    def test_mocked_plan_reports_non_intercepted_tools_as_live(self, tmp_path: Path) -> None:
        # ADR-0300: mocked replay serves only MCP tools/call from the capsule; a
        # recorded call without `transport: mcp` runs live, and the plan says so
        # (it used to claim every tool would be mocked).
        sid, root, caps, _ = build(tmp_path, n=2, tools=self.TOOLS)
        plan = plan_session_replay(sid, root=root, capsule_base=caps, from_seq=1)
        assert plan.range == (1, 1)
        assert plan.to_json_dict()["range"] == {"from": 1, "to": 1}
        exposure = plan.turns[0].tool_exposure
        assert exposure is not None
        assert exposure.decisions == {"live": 1}
        assert exposure.mutating == 1

    def test_plan_predicts_integrity_refusals(self, tmp_path: Path) -> None:
        sid, root, caps, _ = build(tmp_path, n=2)
        (caps / RUNS[1] / "capsule.yaml").write_text("run_id: changed\n")
        plan = plan_session_replay(sid, root=root, capsule_base=caps)
        assert [t.integrity for t in plan.turns] == ["ok", "tampered"]
        assert [t.would_refuse for t in plan.turns] == [False, True]
        assert plan.turns[1].tool_exposure is None

    def test_malformed_replay_policy_never_fails_plan(self, tmp_path: Path) -> None:
        sid, root, caps, _ = build(tmp_path, n=1, tools=self.TOOLS)
        (caps / RUNS[0] / "replay.yaml").write_text("tool_overrides: [unclosed\n")
        plan = plan_session_replay(sid, root=root, capsule_base=caps)
        assert plan.turns[0].tool_exposure is None

    def test_plan_applies_same_validation(self, tmp_path: Path) -> None:
        sid, root, caps, _ = build(tmp_path, n=2)
        with pytest.raises(SessionReplayRangeError):
            plan_session_replay(sid, root=root, capsule_base=caps, from_seq=4)
