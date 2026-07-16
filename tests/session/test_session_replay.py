"""ADR-0123 session replay: ordered orchestration over the per-capsule engine.

Behavioral invariants under test (session-replay-v0 §How a session maps to a
replay + §Acceptance criteria, P1 scope):

- members replay in ascending ``sequence``; gaps/duplicates/empty refuse;
- every replayed turn is recorded, in order, with a per-turn verdict;
- a missing or tampered member is an honest hard refusal (never skipped
  silently), halting unless ``continue_past_refusal``;
- a soft divergence halts under ``on_divergence="stop"`` and continues under
  ``"continue"``; halted runs leave later turns absent (not ``skipped``);
- the emitted record validates against the graduated
  ``schemas/session-replay-result.schema.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from novafabric.session import (
    MemberRun,
    SessionIntegrityError,
    SessionNotFoundError,
    SessionReplayError,
    SessionReplayResult,
    add_member,
    new_session,
    replay_session,
    save_session,
    session_manifest_path,
    write_session_replay_result,
)

REPO_ROOT = Path(__file__).parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "session-replay"
SCHEMA_PATH = REPO_ROOT / "schemas" / "session-replay-result.schema.json"

ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")

RUN_1 = "01HZ8T0A00YZ2K7N9DPBYK2W01"
RUN_2 = "01HZ8T1B00YZ2K7N9DPBYK2W02"

PASS_CMD = [sys.executable, "-c", "pass"]
FAIL_CMD = [sys.executable, "-c", "import sys; sys.exit(5)"]


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


# ---------------------------------------------------------------------------
# Graduated schema: all 14 golden fixtures behave as their filename asserts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURE_DIR.glob("*.json")))
def test_golden_fixture_behaves_as_named(fixture: str) -> None:
    record = json.loads((FIXTURE_DIR / fixture).read_text())
    errors = list(_validator().iter_errors(record))
    if fixture.startswith("valid-"):
        assert errors == [], f"{fixture}: unexpected errors: {[e.message for e in errors]}"
    else:
        assert errors, f"{fixture}: expected schema rejection, got none"


def test_fixture_count_matches_graduated_set() -> None:
    names = sorted(p.name for p in FIXTURE_DIR.glob("*.json"))
    assert len(names) == 14
    assert sum(1 for n in names if n.startswith("valid-")) == 5


# ---------------------------------------------------------------------------
# Fake member capsules (with a re-executable command for mocked replay)
# ---------------------------------------------------------------------------


def make_capsule(
    base: Path,
    run_id: str,
    command: list[str] | None = None,
) -> Path:
    capsule_dir = base / run_id
    capsule_dir.mkdir(parents=True)
    manifest: dict[str, object] = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "created_at": "2026-07-15T09:00:00.000000Z",
        "finished_at": "2026-07-15T09:00:01.500000Z",
        "duration_ms": 1500,
        "status": "success",
    }
    if command is not None:
        manifest["command"] = command
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))
    (capsule_dir / "model-calls.jsonl").write_text("")
    return capsule_dir


def build_session(
    tmp_path: Path,
    commands: dict[str, list[str] | None],
) -> tuple[str, Path, Path, Path]:
    """One session over len(commands) fresh capsules; returns key paths."""
    session_root = tmp_path / "sessions"
    caps = tmp_path / "caps"
    replays = tmp_path / "replays"
    manifest = new_session(kind="conversation")
    save_session(manifest, root=session_root)
    for run_id, command in commands.items():
        capsule_dir = make_capsule(caps, run_id, command=command)
        add_member(manifest, capsule_dir, root=session_root)
    save_session(manifest, root=session_root)
    return manifest.session_id, session_root, caps, replays


# ---------------------------------------------------------------------------
# Full reproduction (P1 happy paths)
# ---------------------------------------------------------------------------


class TestFullReproduction:
    def test_two_turn_mocked_session_reproduced_in_order(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(
            tmp_path, {RUN_1: PASS_CMD, RUN_2: PASS_CMD}
        )
        result = replay_session(
            sid, root=session_root, capsule_base=caps, base_dir=replays
        )
        assert result.whole_session_verdict == "reproduced"
        assert result.mode == "mocked"
        assert result.on_divergence == "stop"
        assert [t.sequence for t in result.turns] == [0, 1]
        assert [t.source_capsule_id for t in result.turns] == [RUN_1, RUN_2]
        assert all(t.status == "reproduced" for t in result.turns)
        assert all(t.effective_mode == "mocked" for t in result.turns)
        assert all(t.divergence is None for t in result.turns)
        # each turn produced its own replay capsule via the existing engine
        for turn in result.turns:
            assert turn.replay_capsule_id is not None
            assert ULID_RE.match(turn.replay_capsule_id)
            assert (replays / turn.replay_capsule_id / "replay_result.yaml").is_file()
        # state-seam verification is ADR-0123 P2 (future design): null today
        assert all(t.state_in_hash is None for t in result.turns)
        assert all(t.state_out_hash is None for t in result.turns)
        assert all(t.state_seam_match is None for t in result.turns)

    def test_manifest_hash_pins_manifest_at_replay_time(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(tmp_path, {RUN_1: PASS_CMD})
        result = replay_session(
            sid, root=session_root, capsule_base=caps, base_dir=replays
        )
        raw = session_manifest_path(sid, session_root).read_bytes()
        assert result.session_manifest_hash == (
            "sha256:" + hashlib.sha256(raw).hexdigest()
        )

    def test_single_turn_session_reproduces(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(tmp_path, {RUN_1: PASS_CMD})
        result = replay_session(
            sid, root=session_root, capsule_base=caps, base_dir=replays
        )
        assert result.whole_session_verdict == "reproduced"
        assert len(result.turns) == 1
        assert result.turns[0].state_seam_match is None  # no successor

    def test_forensic_mode_needs_no_command(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(
            tmp_path, {RUN_1: None, RUN_2: None}
        )
        result = replay_session(
            sid, mode="forensic", root=session_root, capsule_base=caps, base_dir=replays
        )
        assert result.whole_session_verdict == "reproduced"
        assert [t.effective_mode for t in result.turns] == ["forensic", "forensic"]

    def test_emitted_record_validates_against_graduated_schema(
        self, tmp_path: Path
    ) -> None:
        sid, session_root, caps, replays = build_session(
            tmp_path, {RUN_1: PASS_CMD, RUN_2: PASS_CMD}
        )
        result = replay_session(
            sid, root=session_root, capsule_base=caps, base_dir=replays
        )
        _validator().validate(result.to_json_dict())


# ---------------------------------------------------------------------------
# Refusals: missing/tampered members, exact preconditions, no command
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_missing_member_refuses_and_halts(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(
            tmp_path, {RUN_1: PASS_CMD, RUN_2: PASS_CMD}
        )
        shutil.rmtree(caps / RUN_1)
        result = replay_session(
            sid, root=session_root, capsule_base=caps, base_dir=replays
        )
        assert result.whole_session_verdict == "refused"
        # halted at the refusal: the later turn is absent, not "skipped"
        assert len(result.turns) == 1
        turn = result.turns[0]
        assert turn.status == "refused"
        assert turn.replay_capsule_id is None
        assert turn.divergence is not None
        assert turn.divergence["kind"] == "precondition_refusal"
        assert "could not be located" in turn.divergence["detail"]
        _validator().validate(result.to_json_dict())

    def test_tampered_member_refuses(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(
            tmp_path, {RUN_1: PASS_CMD, RUN_2: PASS_CMD}
        )
        capsule_yaml = caps / RUN_2 / "capsule.yaml"
        capsule_yaml.write_text(capsule_yaml.read_text() + "\ntampered_field: true\n")
        result = replay_session(
            sid, root=session_root, capsule_base=caps, base_dir=replays
        )
        assert result.whole_session_verdict == "refused"
        assert [t.status for t in result.turns] == ["reproduced", "refused"]
        divergence = result.turns[1].divergence
        assert divergence is not None
        assert divergence["kind"] == "precondition_refusal"
        assert "tampered" in divergence["detail"]

    def test_continue_past_refusal_records_override_and_later_turns(
        self, tmp_path: Path
    ) -> None:
        sid, session_root, caps, replays = build_session(
            tmp_path, {RUN_1: PASS_CMD, RUN_2: PASS_CMD}
        )
        shutil.rmtree(caps / RUN_1)
        result = replay_session(
            sid,
            continue_past_refusal=True,
            root=session_root,
            capsule_base=caps,
            base_dir=replays,
        )
        assert [t.status for t in result.turns] == ["refused", "reproduced"]
        # a refusal poisons the whole-session verdict even when overridden
        assert result.whole_session_verdict == "refused"
        assert result.to_json_dict()["continue_past_refusal"] is True
        _validator().validate(result.to_json_dict())

    def test_exact_mode_precondition_failure_refuses(self, tmp_path: Path) -> None:
        # no env.lock => not deterministic => exact_eligible is False
        sid, session_root, caps, replays = build_session(tmp_path, {RUN_1: PASS_CMD})
        result = replay_session(
            sid, mode="exact", root=session_root, capsule_base=caps, base_dir=replays
        )
        assert result.whole_session_verdict == "refused"
        divergence = result.turns[0].divergence
        assert divergence is not None
        assert divergence["kind"] == "precondition_refusal"
        assert "exact" in divergence["detail"]

    def test_mocked_capsule_without_command_refuses(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(tmp_path, {RUN_1: None})
        result = replay_session(
            sid, root=session_root, capsule_base=caps, base_dir=replays
        )
        assert result.whole_session_verdict == "refused"
        assert result.turns[0].status == "refused"
        divergence = result.turns[0].divergence
        assert divergence is not None
        assert "command" in divergence["detail"]


# ---------------------------------------------------------------------------
# Divergence policy (D5): stop by default, continue on request
# ---------------------------------------------------------------------------


class TestDivergencePolicy:
    def test_divergence_stops_by_default(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(
            tmp_path, {RUN_1: FAIL_CMD, RUN_2: PASS_CMD}
        )
        result = replay_session(
            sid, root=session_root, capsule_base=caps, base_dir=replays
        )
        assert result.whole_session_verdict == "diverged"
        assert len(result.turns) == 1  # later turn absent, not "skipped"
        turn = result.turns[0]
        assert turn.status == "diverged"
        assert turn.replay_capsule_id is not None  # the divergent replay exists
        assert turn.divergence is not None
        assert turn.divergence["kind"] == "replay_failed"
        _validator().validate(result.to_json_dict())

    def test_on_divergence_continue_replays_later_turns(self, tmp_path: Path) -> None:
        sid, session_root, caps, replays = build_session(
            tmp_path, {RUN_1: FAIL_CMD, RUN_2: PASS_CMD}
        )
        result = replay_session(
            sid,
            on_divergence="continue",
            root=session_root,
            capsule_base=caps,
            base_dir=replays,
        )
        assert result.whole_session_verdict == "diverged"
        assert [t.status for t in result.turns] == ["diverged", "reproduced"]
        assert result.on_divergence == "continue"


# ---------------------------------------------------------------------------
# Whole-session refusals: empty, gaps, unknown session
# ---------------------------------------------------------------------------


class TestSessionLevelRefusals:
    def test_empty_session_refuses(self, tmp_path: Path) -> None:
        session_root = tmp_path / "sessions"
        manifest = new_session()
        save_session(manifest, root=session_root)
        with pytest.raises(SessionReplayError, match="nothing to replay"):
            replay_session(manifest.session_id, root=session_root)

    def test_sequence_gap_refuses(self, tmp_path: Path) -> None:
        session_root = tmp_path / "sessions"
        caps = tmp_path / "caps"
        manifest = new_session()
        save_session(manifest, root=session_root)
        for run_id in (RUN_1, RUN_2):
            add_member(manifest, make_capsule(caps, run_id), root=session_root)
        manifest.member_runs[1] = MemberRun(
            **{**manifest.member_runs[1].to_json_dict(), "sequence": 3}
        )
        save_session(manifest, root=session_root)  # gap is legal in the manifest
        with pytest.raises(SessionIntegrityError, match="gaps"):
            replay_session(manifest.session_id, root=session_root, capsule_base=caps)

    def test_unknown_session_raises_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(SessionNotFoundError):
            replay_session("01HZ8S9K3M4YZ2K7N9DPBYK2W0", root=tmp_path / "sessions")


# ---------------------------------------------------------------------------
# Result persistence
# ---------------------------------------------------------------------------


def test_write_session_replay_result_round_trips(tmp_path: Path) -> None:
    sid, session_root, caps, replays = build_session(tmp_path, {RUN_1: PASS_CMD})
    result = replay_session(sid, root=session_root, capsule_base=caps, base_dir=replays)
    path = write_session_replay_result(result, tmp_path / "out")
    assert path.name == "session_replay_result.json"
    loaded = json.loads(path.read_text())
    assert loaded == result.to_json_dict()
    assert SessionReplayResult.model_validate(loaded).session_id == sid
