"""ADR-0122 session manifest: schema, model, store, assembly, stats.

Behavioral invariants under test (session-capsule-v0 §Edge cases):
- sequence values unique + ascending; violations are named errors;
- a dangling member is reported ``missing``, a hash-mismatched one
  ``tampered`` — never an exception;
- the session layer only reads member capsules (one capsule, one writer);
- a pre-ADR-0122 capsule (no session fields) is a perfectly good member.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import jsonschema
import pytest
import yaml

from novafabric.session import (
    DuplicateMemberError,
    MemberRun,
    NotACapsuleError,
    SessionFinalizedError,
    SessionIntegrityError,
    SessionManifest,
    SessionNotFoundError,
    add_member,
    capsule_manifest_digest,
    list_sessions,
    load_session,
    new_session,
    resolve_members,
    save_session,
    session_manifest_path,
    session_stats,
    sessions_root,
)
from novafabric.session.manifest import parse_capsule_ref, validate_ordering

REPO_ROOT = Path(__file__).parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "session-manifest"
SCHEMA_PATH = REPO_ROOT / "schemas" / "session-manifest.schema.json"

SID = "01HZ8S9K3M4YZ2K7N9DPBYK2W0"


# ---------------------------------------------------------------------------
# Graduated schema: all 11 golden fixtures behave as their filename asserts
# ---------------------------------------------------------------------------


def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


@pytest.mark.parametrize("fixture", sorted(p.name for p in FIXTURE_DIR.glob("*.json")))
def test_golden_fixture_behaves_as_named(fixture: str) -> None:
    manifest = json.loads((FIXTURE_DIR / fixture).read_text())
    errors = list(_validator().iter_errors(manifest))
    if fixture.startswith("valid-"):
        assert errors == [], f"{fixture}: unexpected errors: {[e.message for e in errors]}"
    else:
        assert errors, f"{fixture}: expected schema rejection, got none"


def test_fixture_count_matches_graduated_set() -> None:
    names = sorted(p.name for p in FIXTURE_DIR.glob("*.json"))
    assert len(names) == 11
    assert sum(1 for n in names if n.startswith("valid-")) == 4


def test_saved_manifest_validates_against_graduated_schema(tmp_path: Path) -> None:
    manifest = new_session(kind="workflow", user_ref="user:sha256:abc")
    path = save_session(manifest, root=tmp_path)
    _validator().validate(json.loads(path.read_text()))


# ---------------------------------------------------------------------------
# Fake member capsules (never touched by the session layer after creation)
# ---------------------------------------------------------------------------


def make_capsule(
    base: Path,
    run_id: str,
    created_at: str = "2026-07-15T09:00:00.000000Z",
    duration_ms: int = 1500,
    status: str = "success",
    cost_usd: float | None = None,
    **extra_fields: object,
) -> Path:
    capsule_dir = base / run_id
    capsule_dir.mkdir(parents=True)
    manifest: dict[str, object] = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "created_at": created_at,
        "finished_at": "2026-07-15T09:00:01.500000Z",
        "duration_ms": duration_ms,
        "status": status,
        "usage_totals": {"total_tokens": 100},
        **extra_fields,
    }
    (capsule_dir / "capsule.yaml").write_text(yaml.dump(manifest))
    lines = []
    if cost_usd is not None:
        lines.append(json.dumps({"nova.cost": {"amount": cost_usd, "currency": "USD"}}))
    (capsule_dir / "model-calls.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))
    return capsule_dir


RUN_1 = "01HZ8T0A00YZ2K7N9DPBYK2W01"
RUN_2 = "01HZ8T1B00YZ2K7N9DPBYK2W02"
RUN_3 = "01HZ8T2C00YZ2K7N9DPBYK2W03"


# ---------------------------------------------------------------------------
# Model / store round-trip
# ---------------------------------------------------------------------------


class TestStore:
    def test_new_session_id_is_ulid_and_empty(self) -> None:
        manifest = new_session()
        assert len(manifest.session_id) == 26
        assert manifest.member_runs == []
        assert manifest.session_kind == "conversation"

    def test_save_load_round_trip(self, tmp_path: Path) -> None:
        manifest = new_session(kind="custom", metadata={"ticket": "SUP-1"})
        save_session(manifest, root=tmp_path)
        loaded = load_session(manifest.session_id, root=tmp_path)
        assert loaded == manifest

    def test_unknown_session_raises_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(SessionNotFoundError, match="unknown session"):
            load_session(SID, root=tmp_path)

    def test_sessions_root_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOVAFABRIC_SESSION_DIR", "/tmp/somewhere")
        assert sessions_root() == Path("/tmp/somewhere")

    def test_sessions_root_defaults_under_nova_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
        assert sessions_root() == tmp_path / "sessions"

    def test_list_sessions_newest_first_and_skips_unreadable(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        first = new_session()
        second = new_session()
        save_session(first, root=tmp_path)
        save_session(second, root=tmp_path)
        broken = tmp_path / "01HZZZZZZZZZZZZZZZZZZZZZZ0"
        broken.mkdir()
        (broken / "session.json").write_text("{not json")
        with caplog.at_level(logging.WARNING, logger="novafabric.session.manifest"):
            manifests = list_sessions(root=tmp_path)
        assert [m.session_id for m in manifests] == sorted(
            [first.session_id, second.session_id], reverse=True
        )
        assert any("Skipping unreadable" in r.message for r in caplog.records)

    def test_list_sessions_empty_root(self, tmp_path: Path) -> None:
        assert list_sessions(root=tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# Ordering invariants (cross-item; not expressible in JSON Schema)
# ---------------------------------------------------------------------------


def _member(run_id: str, sequence: int) -> MemberRun:
    return MemberRun(
        run_id=run_id,
        capsule_ref="sha256:" + "0" * 64,
        sequence=sequence,
        started_at="2026-07-15T09:00:00.000000Z",
    )


class TestOrdering:
    def test_duplicate_sequence_rejected(self) -> None:
        manifest = new_session()
        manifest.member_runs = [_member(RUN_1, 0), _member(RUN_2, 0)]
        with pytest.raises(SessionIntegrityError, match="duplicate sequence"):
            validate_ordering(manifest)

    def test_out_of_order_sequence_rejected(self) -> None:
        manifest = new_session()
        manifest.member_runs = [_member(RUN_1, 1), _member(RUN_2, 0)]
        with pytest.raises(SessionIntegrityError, match="not sorted"):
            validate_ordering(manifest)

    def test_save_refuses_broken_ordering(self, tmp_path: Path) -> None:
        manifest = new_session()
        manifest.member_runs = [_member(RUN_1, 1), _member(RUN_2, 1)]
        with pytest.raises(SessionIntegrityError):
            save_session(manifest, root=tmp_path)

    def test_load_refuses_broken_ordering(self, tmp_path: Path) -> None:
        manifest = new_session()
        save_session(manifest, root=tmp_path)
        path = session_manifest_path(manifest.session_id, root=tmp_path)
        raw = json.loads(path.read_text())
        raw["member_runs"] = [
            _member(RUN_1, 1).to_json_dict(),
            _member(RUN_2, 0).to_json_dict(),
        ]
        path.write_text(json.dumps(raw))
        with pytest.raises(SessionIntegrityError):
            load_session(manifest.session_id, root=tmp_path)


# ---------------------------------------------------------------------------
# add_member: references, auto-sequence, refusals
# ---------------------------------------------------------------------------


class TestAddMember:
    def test_appends_in_order_with_content_ref(self, tmp_path: Path) -> None:
        manifest = new_session()
        save_session(manifest, root=tmp_path)
        caps = tmp_path / "caps"
        for i, run_id in enumerate([RUN_1, RUN_2, RUN_3]):
            capsule = make_capsule(caps, run_id, created_at=f"2026-07-15T09:0{i}:00Z")
            member = add_member(manifest, capsule, root=tmp_path)
            assert member.sequence == i
            assert member.started_at == f"2026-07-15T09:0{i}:00Z"
            path_prefix, digest = parse_capsule_ref(member.capsule_ref)
            assert path_prefix is not None
            assert digest == capsule_manifest_digest(capsule)
        save_session(manifest, root=tmp_path)
        loaded = load_session(manifest.session_id, root=tmp_path)
        assert [m.run_id for m in loaded.member_runs] == [RUN_1, RUN_2, RUN_3]

    def test_old_capsule_without_session_fields_is_fine(self, tmp_path: Path) -> None:
        """A pre-ADR-0122 capsule needs no back-reference to join a session."""
        manifest = new_session()
        capsule = make_capsule(tmp_path / "caps", RUN_1)
        assert "session_id" not in yaml.safe_load((capsule / "capsule.yaml").read_text())
        member = add_member(manifest, capsule, root=tmp_path)
        assert member.run_id == RUN_1

    def test_add_never_writes_the_member_capsule(self, tmp_path: Path) -> None:
        manifest = new_session()
        capsule = make_capsule(tmp_path / "caps", RUN_1)
        before = (capsule / "capsule.yaml").read_bytes()
        add_member(manifest, capsule, root=tmp_path)
        assert (capsule / "capsule.yaml").read_bytes() == before

    def test_duplicate_run_id_rejected(self, tmp_path: Path) -> None:
        manifest = new_session()
        capsule = make_capsule(tmp_path / "caps", RUN_1)
        add_member(manifest, capsule, root=tmp_path)
        with pytest.raises(DuplicateMemberError):
            add_member(manifest, capsule, root=tmp_path)

    def test_not_a_capsule_rejected(self, tmp_path: Path) -> None:
        manifest = new_session()
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(NotACapsuleError):
            add_member(manifest, empty, root=tmp_path)

    def test_finalized_session_refuses_add_unless_reopened(self, tmp_path: Path) -> None:
        manifest = new_session()
        manifest.finalized_at = "2026-07-15T10:00:00.000000Z"
        capsule = make_capsule(tmp_path / "caps", RUN_1)
        with pytest.raises(SessionFinalizedError):
            add_member(manifest, capsule, root=tmp_path)
        member = add_member(manifest, capsule, root=tmp_path, reopen=True)
        assert member.sequence == 0

    def test_capsule_side_disagreement_warns_manifest_wins(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        manifest = new_session()
        capsule = make_capsule(
            tmp_path / "caps", RUN_1, session_id=SID, sequence=9
        )
        with caplog.at_level(logging.WARNING, logger="novafabric.session.manifest"):
            member = add_member(manifest, capsule, root=tmp_path)
        assert member.sequence == 0  # manifest is authoritative
        assert any("authoritative" in r.message for r in caplog.records)

    def test_role_recorded(self, tmp_path: Path) -> None:
        manifest = new_session()
        capsule = make_capsule(tmp_path / "caps", RUN_1)
        member = add_member(manifest, capsule, root=tmp_path, role="user-turn")
        assert member.role == "user-turn"


# ---------------------------------------------------------------------------
# resolve_members + session_stats: assembly, dangling/tampered, aggregates
# ---------------------------------------------------------------------------


def _session_with_three_members(tmp_path: Path) -> SessionManifest:
    manifest = new_session()
    save_session(manifest, root=tmp_path)
    caps = tmp_path / "caps"
    make_capsule(caps, RUN_1, cost_usd=0.25)
    make_capsule(caps, RUN_2, cost_usd=0.50)
    make_capsule(caps, RUN_3, status="failure")
    for run_id in (RUN_1, RUN_2, RUN_3):
        add_member(manifest, caps / run_id, root=tmp_path)
    save_session(manifest, root=tmp_path)
    return manifest


class TestResolveAndStats:
    def test_all_members_resolve_ok_in_sequence_order(self, tmp_path: Path) -> None:
        manifest = _session_with_three_members(tmp_path)
        resolved = resolve_members(manifest, root=tmp_path)
        assert [r.status for r in resolved] == ["ok", "ok", "ok"]
        assert [r.member.sequence for r in resolved] == [0, 1, 2]
        assert resolved[2].run_status == "failure"
        assert resolved[0].total_tokens == 100
        assert resolved[0].cost_by_currency == {"USD": 0.25}

    def test_deleted_member_reported_missing_not_fatal(self, tmp_path: Path) -> None:
        manifest = _session_with_three_members(tmp_path)
        import shutil

        shutil.rmtree(tmp_path / "caps" / RUN_2)
        resolved = resolve_members(manifest, root=tmp_path)
        assert [r.status for r in resolved] == ["ok", "missing", "ok"]
        assert resolved[1].member.run_id == RUN_2

    def test_modified_member_reported_tampered(self, tmp_path: Path) -> None:
        manifest = _session_with_three_members(tmp_path)
        target = tmp_path / "caps" / RUN_1 / "capsule.yaml"
        target.write_text(target.read_text() + "\n# post-hoc edit\n")
        resolved = resolve_members(manifest, root=tmp_path)
        assert resolved[0].status == "tampered"
        assert resolved[0].capsule_dir is not None

    def test_bare_digest_ref_resolved_via_capsule_base(self, tmp_path: Path) -> None:
        manifest = new_session()
        save_session(manifest, root=tmp_path)
        caps = tmp_path / "caps"
        capsule = make_capsule(caps, RUN_1)
        member = add_member(manifest, capsule, root=tmp_path)
        # Rewrite as a bare-digest ref (spec-valid): only capsule_base can find it.
        _, digest = parse_capsule_ref(member.capsule_ref)
        member.capsule_ref = digest
        resolved = resolve_members(manifest, root=tmp_path, capsule_base=caps)
        assert resolved[0].status == "ok"
        without_base = resolve_members(manifest, root=tmp_path)
        assert without_base[0].status == "missing"

    def test_stats_aggregate_turns_duration_tokens_cost(self, tmp_path: Path) -> None:
        manifest = _session_with_three_members(tmp_path)
        stats = session_stats(resolve_members(manifest, root=tmp_path))
        assert (stats.turns, stats.resolved, stats.missing, stats.tampered) == (3, 3, 0, 0)
        assert stats.total_duration_ms == 4500
        assert stats.total_tokens == 300
        assert stats.cost_by_currency == {"USD": 0.75}
        assert stats.first_started_at is not None

    def test_stats_skip_unresolvable_members(self, tmp_path: Path) -> None:
        manifest = _session_with_three_members(tmp_path)
        import shutil

        shutil.rmtree(tmp_path / "caps" / RUN_2)
        stats = session_stats(resolve_members(manifest, root=tmp_path))
        assert (stats.resolved, stats.missing) == (2, 1)
        assert stats.total_duration_ms == 3000
        assert stats.total_tokens == 200

    def test_empty_session_stats(self, tmp_path: Path) -> None:
        manifest = new_session()
        save_session(manifest, root=tmp_path)
        stats = session_stats(resolve_members(manifest, root=tmp_path))
        assert stats.turns == 0
        assert stats.total_duration_ms is None
        assert stats.cost_by_currency is None
