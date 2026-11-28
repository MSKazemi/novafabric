"""Scope and tool behaviour of the read-only MCP evidence server (issue #244).

The safety properties under test:

* the scope is exactly the directories the operator named — a client can name a
  capsule only as one direct child, never a path, a ``..``, or a link out;
* nothing a tool returns carries prompt/response content, the command line, the
  host, or the scope's absolute path, and secrets in returned strings are masked;
* verification fails closed: unsealed, tampered and planted-link capsules are
  never ``valid``;
* no tool writes anything.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from novafabric.mcp import evidence_tools
from novafabric.mcp.evidence_tools import (
    MAX_SCOPE_ROOTS,
    SUMMARY_FIELDS,
    VERIFICATION_LEVEL,
    EvidenceScope,
    EvidenceToolError,
    ScopeError,
)

from .conftest import PROMPT, SECRET, snapshot, write_capsule

# -- scope construction ------------------------------------------------------


def test_scope_requires_at_least_one_directory() -> None:
    with pytest.raises(ScopeError, match="at least one"):
        EvidenceScope.from_paths([])


def test_scope_refuses_a_file_and_a_missing_path(tmp_path: Path) -> None:
    a_file = tmp_path / "f"
    a_file.write_text("x")
    with pytest.raises(ScopeError, match="not a directory"):
        EvidenceScope.from_paths([a_file])
    with pytest.raises(ScopeError, match="not a directory"):
        EvidenceScope.from_paths([tmp_path / "missing"])


def test_scope_refuses_the_filesystem_root() -> None:
    with pytest.raises(ScopeError, match="filesystem root"):
        EvidenceScope.from_paths(["/"])


def test_scope_caps_the_number_of_directories(tmp_path: Path) -> None:
    dirs = []
    for i in range(MAX_SCOPE_ROOTS + 1):
        (tmp_path / str(i)).mkdir()
        dirs.append(tmp_path / str(i))
    with pytest.raises(ScopeError, match="at most"):
        EvidenceScope.from_paths(dirs)


def test_scope_is_canonical_and_deduplicated(tmp_path: Path, runs: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(runs)
    scope = EvidenceScope.from_paths([runs, alias, f"{runs}/."])
    assert scope.roots == (Path(os.path.realpath(runs)),)


# -- addressing and scope escapes -------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["../runs", "..", ".", "/etc", "a/b", "a\\b", "", "x\x00y", "x\ny", "‮evil", "a" * 300],
)
def test_a_name_that_is_not_one_segment_is_refused(runs: Path, name: str) -> None:
    write_capsule(runs, "ok")
    scope = EvidenceScope.from_paths([runs])
    with pytest.raises(EvidenceToolError) as info:
        scope.get_capsule_summary(name)
    assert info.value.code == "invalid_argument"


@pytest.mark.parametrize("value", [None, 7, ["a"], {"a": 1}, True])
def test_a_capsule_that_is_not_a_string_is_refused(runs: Path, value: object) -> None:
    scope = EvidenceScope.from_paths([runs])
    with pytest.raises(EvidenceToolError) as info:
        scope.verify_capsule(value)
    assert info.value.code == "invalid_argument"


def test_a_link_to_a_capsule_outside_the_scope_is_refused(tmp_path: Path, runs: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    write_capsule(outside, "secret-run")
    (runs / "escape").symlink_to(outside / "secret-run")
    scope = EvidenceScope.from_paths([runs])
    for call in (scope.get_capsule_summary, scope.verify_capsule):
        with pytest.raises(EvidenceToolError) as info:
            call("escape")
        assert info.value.code == "outside_scope"
        assert str(tmp_path) not in info.value.message
    assert scope.list_capsules()["capsules"] == []


def test_a_link_to_a_capsule_inside_the_scope_is_refused_too(runs: Path) -> None:
    write_capsule(runs, "real")
    (runs / "latest").symlink_to(runs / "real")
    scope = EvidenceScope.from_paths([runs])
    with pytest.raises(EvidenceToolError) as info:
        scope.get_capsule_summary("latest")
    assert info.value.code == "outside_scope"
    assert [c["capsule"] for c in scope.list_capsules()["capsules"]] == ["real"]


def test_a_directory_without_a_manifest_is_not_a_capsule(runs: Path) -> None:
    (runs / "plain").mkdir()
    (runs / "afile").write_text("x")
    scope = EvidenceScope.from_paths([runs])
    for name in ("plain", "afile", "missing"):
        with pytest.raises(EvidenceToolError) as info:
            scope.get_capsule_summary(name)
        assert info.value.code == "capsule_not_found"


def test_a_linked_manifest_marker_does_not_make_a_capsule(tmp_path: Path, runs: Path) -> None:
    target = tmp_path / "elsewhere.yaml"
    target.write_text(yaml.safe_dump({"run_id": "x"}))
    (runs / "fake").mkdir()
    (runs / "fake" / "capsule.yaml").symlink_to(target)
    scope = EvidenceScope.from_paths([runs])
    with pytest.raises(EvidenceToolError) as info:
        scope.get_capsule_summary("fake")
    assert info.value.code == "capsule_not_found"


def test_same_name_in_two_scopes_needs_root(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    write_capsule(a, "run")
    write_capsule(b, "run", status="failed")
    scope = EvidenceScope.from_paths([a, b])
    with pytest.raises(EvidenceToolError) as info:
        scope.get_capsule_summary("run")
    assert info.value.code == "ambiguous_capsule"
    assert scope.get_capsule_summary("run", root=1)["fields"]["status"] == "failed"


@pytest.mark.parametrize("root", [-1, 2, True, "0", 1.0])
def test_root_must_be_a_valid_scope_index(tmp_path: Path, root: object) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    write_capsule(a, "run")
    b.mkdir()
    scope = EvidenceScope.from_paths([a, b])
    with pytest.raises(EvidenceToolError) as info:
        scope.get_capsule_summary("run", root=root)
    assert info.value.code == "invalid_argument"


# -- list_capsules -----------------------------------------------------------


def test_list_pages_with_an_opaque_cursor(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for name in ("r1", "r2", "r3"):
        write_capsule(a, name)
    write_capsule(b, "r0")
    scope = EvidenceScope.from_paths([a, b])
    seen: list[tuple[int, str]] = []
    cursor = None
    pages = 0
    while True:
        page = scope.list_capsules(cursor=cursor, limit=2)
        seen += [(c["root"], c["capsule"]) for c in page["capsules"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == [(0, "r1"), (0, "r2"), (0, "r3"), (1, "r0")]
    assert pages == 2
    assert [c["capsule"] for c in scope.list_capsules(root=1)["capsules"]] == ["r0"]


def test_list_reports_seal_presence(runs: Path, sealed: Callable[[Path, str], Path]) -> None:
    write_capsule(runs, "plain")
    sealed(runs, "signed")
    rows = EvidenceScope.from_paths([runs]).list_capsules()["capsules"]
    assert {r["capsule"]: r["sealed"] for r in rows} == {"plain": False, "signed": True}


@pytest.mark.parametrize("cursor", ["", "x", "9/r1", "a/r1", "0/", 5, ["0/r1"]])
def test_a_forged_cursor_is_refused(runs: Path, cursor: object) -> None:
    write_capsule(runs, "r1")
    with pytest.raises(EvidenceToolError) as info:
        EvidenceScope.from_paths([runs]).list_capsules(cursor=cursor)
    assert info.value.code == "invalid_argument"


@pytest.mark.parametrize("limit", [0, 201, -3, "10", 2.5, True])
def test_limit_is_bounded(runs: Path, limit: object) -> None:
    with pytest.raises(EvidenceToolError) as info:
        EvidenceScope.from_paths([runs]).list_capsules(limit=limit)
    assert info.value.code == "invalid_argument"


# -- get_capsule_summary -----------------------------------------------------


def test_summary_returns_allowlisted_fields_and_names_the_rest(tmp_path: Path, runs: Path) -> None:
    write_capsule(runs, "r1", metadata={"note": PROMPT}, error=PROMPT, usage_totals={"tokens": 9})
    summary = EvidenceScope.from_paths([runs]).get_capsule_summary("r1")
    assert set(summary["fields"]) <= set(SUMMARY_FIELDS)
    assert summary["fields"]["run_id"] == "r1"
    assert summary["fields"]["usage_totals"] == {"tokens": 9}
    assert {"command", "host", "working_directory", "metadata", "error"} <= set(
        summary["withheld_fields"]
    )
    assert summary["signature_checked"] is False
    assert summary["sealed"] is False
    text = json.dumps(summary)
    for leaked in (PROMPT, SECRET, "build-host-7", str(tmp_path), "agent.py"):
        assert leaked not in text


def test_summary_masks_secrets_and_clips_long_strings(runs: Path) -> None:
    write_capsule(runs, "r1", alias=f"key {SECRET}", session_id="s" * 5000)
    fields = EvidenceScope.from_paths([runs]).get_capsule_summary("r1")["fields"]
    assert SECRET not in fields["alias"]
    assert "[REDACTED:" in fields["alias"]
    assert len(fields["session_id"]) <= evidence_tools.MAX_STRING_CHARS
    assert fields["session_id"].endswith("[truncated]")


def test_summary_bounds_nesting_and_serialises_yaml_timestamps(runs: Path) -> None:
    capsule = runs / "r1"
    capsule.mkdir()
    deep: dict[str, object] = {"leaf": 1}
    for _ in range(6):
        deep = {"d": deep}
    (capsule / "capsule.yaml").write_text(
        "run_id: r1\ncreated_at: 2026-11-27 01:02:03\nduration_ms: .nan\n"
        + yaml.safe_dump({"usage_totals": deep})
    )
    fields = EvidenceScope.from_paths([runs]).get_capsule_summary("r1")["fields"]
    assert fields["created_at"].startswith("2026-11-27")
    assert fields["duration_ms"] == "nan"
    assert "[nested value omitted]" in json.dumps(fields["usage_totals"])
    json.dumps(fields, allow_nan=False)  # always strict JSON


def test_a_sealed_summary_reads_the_signed_payload_not_an_edited_copy(
    runs: Path, sealed: Callable[[Path, str], Path]
) -> None:
    capsule = sealed(runs, "r1")
    data = yaml.safe_load((capsule / "capsule.yaml").read_text())
    data["status"] = "forged"
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(data))
    summary = EvidenceScope.from_paths([runs]).get_capsule_summary("r1")
    assert summary["manifest_source"] == ".seal/manifest.dsse"
    assert summary["fields"]["status"] == "success"
    assert summary["evidence_files"]["names"] == ["model-calls.jsonl"]


def test_an_unusable_manifest_is_refused_not_guessed(runs: Path) -> None:
    capsule = runs / "r1"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text("- a list\n- not a mapping\n")
    with pytest.raises(EvidenceToolError) as info:
        EvidenceScope.from_paths([runs]).get_capsule_summary("r1")
    assert info.value.code == "capsule_unreadable"


# -- verify_capsule ----------------------------------------------------------


def test_an_unsealed_capsule_is_never_valid(runs: Path) -> None:
    write_capsule(runs, "r1")
    report = EvidenceScope.from_paths([runs]).verify_capsule("r1")
    assert report["valid"] is False
    assert report["sealed"] is False
    assert report["verification_level"] == VERIFICATION_LEVEL


def test_a_sealed_capsule_verifies_and_states_what_is_unanchored(
    tmp_path: Path, runs: Path, sealed: Callable[[Path, str], Path]
) -> None:
    sealed(runs, "r1")
    report = EvidenceScope.from_paths([runs]).verify_capsule("r1")
    assert report["valid"] is True, report["errors"]
    assert report["signature_ok"] is True
    assert report["capsule_binding_ok"] is True
    assert report["identity_trust"] == "self-asserted"
    assert "identity-self-asserted" in report["unanchored_claims"]
    assert report["errors"] == []
    assert str(tmp_path) not in json.dumps(report)


def test_a_capsule_edited_after_sealing_fails(
    runs: Path, sealed: Callable[[Path, str], Path]
) -> None:
    capsule = sealed(runs, "r1")
    with (capsule / "model-calls.jsonl").open("a") as fh:
        fh.write("{}\n")
    report = EvidenceScope.from_paths([runs]).verify_capsule("r1")
    assert report["valid"] is False
    assert report["capsule_binding_ok"] is False
    assert any("model-calls.jsonl" in e for e in report["errors"])


def test_a_corrupted_envelope_fails(runs: Path, sealed: Callable[[Path, str], Path]) -> None:
    capsule = sealed(runs, "r1")
    (capsule / ".seal" / "manifest.dsse").write_bytes(b"{not an envelope")
    report = EvidenceScope.from_paths([runs]).verify_capsule("r1")
    assert report["valid"] is False
    assert report["errors"]


def test_a_linked_seal_dir_is_refused_before_it_is_read(
    tmp_path: Path, runs: Path, sealed: Callable[[Path, str], Path]
) -> None:
    donor = sealed(tmp_path / "donor-root", "donor")
    capsule = write_capsule(runs, "r1")
    (capsule / ".seal").symlink_to(donor / ".seal")
    report = EvidenceScope.from_paths([runs]).verify_capsule("r1")
    assert report["valid"] is False
    assert report["sealed"] is True
    assert "not followed" in report["errors"][0]


def test_a_planted_link_inside_a_sealed_capsule_fails(
    tmp_path: Path, runs: Path, sealed: Callable[[Path, str], Path]
) -> None:
    capsule = sealed(runs, "r1")
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY MATERIAL")
    (capsule / "outputs.txt").symlink_to(secret)
    report = EvidenceScope.from_paths([runs]).verify_capsule("r1")
    assert report["valid"] is False
    assert "PRIVATE KEY MATERIAL" not in json.dumps(report)


def test_verification_errors_are_capped(
    monkeypatch: pytest.MonkeyPatch, runs: Path, sealed: Callable[[Path, str], Path]
) -> None:
    capsule = sealed(runs, "r1")
    # An undecodable envelope fails both the signature and the payload decode.
    (capsule / ".seal" / "manifest.dsse").write_bytes(b"{not an envelope")
    monkeypatch.setattr(evidence_tools, "MAX_ERRORS", 1)
    report = EvidenceScope.from_paths([runs]).verify_capsule("r1")
    assert report["valid"] is False
    assert len(report["errors"]) == 1
    assert report["errors_truncated"] is True


# -- read only ---------------------------------------------------------------


def test_no_tool_writes_anything(
    tmp_path: Path,
    runs: Path,
    sealed: Callable[[Path, str], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_capsule(runs, "plain")
    sealed(runs, "signed")
    before = snapshot(tmp_path)
    opened: list[object] = []
    real_connect = sqlite3.connect

    def recording_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        opened.append(args[0] if args else kwargs.get("database"))
        return real_connect(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sqlite3, "connect", recording_connect)
    scope = EvidenceScope.from_paths([runs])
    scope.list_capsules()
    for name in ("plain", "signed"):
        scope.get_capsule_summary(name)
        scope.verify_capsule(name)
    # Verification from the capsule alone opens no Merkle log or other database.
    assert opened == []
    after = snapshot(tmp_path)
    assert not [p for p in after if p.endswith(("-wal", "-shm", "-journal"))]
    assert after == before
