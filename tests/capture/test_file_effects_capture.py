"""``nova capture`` records the files the workload wrote (ADR-0321 amendment, corpus S6).

Paths and end-of-run sha256 digests only -- never contents. The workload side is an
audit hook in the workload's own Python process; the orchestrator digests what it
handed off. A process that ended without handing off makes the record incomplete,
which replay reads as "not compared", never as "wrote nothing".
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture.file_effects import (
    HANDOFF_PREFIX,
    FileEffectsRecord,
    FileState,
    collect_file_effects,
    display_path,
    file_state,
    parse_record,
    render_record,
    resolve_recorded_path,
    write_intent,
)
from novafabric.cli.main import app


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ── the write-intent rule ────────────────────────────────────────────────────


@pytest.mark.parametrize("mode,flags,expected", [
    ("r", 0, False), ("rb", 0, False), ("w", 0, True), ("ab", 0, True), ("x", 0, True),
    ("r+", 0, True), (None, os.O_RDONLY, False), (None, os.O_WRONLY | os.O_CREAT, True),
    (None, os.O_RDWR, True), (None, os.O_RDONLY | os.O_APPEND, True), (None, True, False),
])
def test_write_intent(mode: object, flags: object, expected: bool) -> None:
    assert write_intent(mode, flags) is expected


# ── the workload-side observer, in a real child interpreter ──────────────────

_CHILD = textwrap.dedent("""
    import os, sys, pathlib
    from novafabric.capture.file_effects import install_file_write_observer
    capsule, work, outside = map(pathlib.Path, sys.argv[1:4])
    assert install_file_write_observer(capsule) is not None
    os.chdir(work)
    open("out.txt", "w").write("hello")                       # relative to cwd
    with open(outside / "ledger.txt", "a") as fh:              # outside the work dir
        fh.write("row\\n")
    pathlib.Path("p.txt").write_text("p")                      # pathlib
    os.close(os.open("raw.bin", os.O_WRONLY | os.O_CREAT))     # os.open
    open("out.txt").read()                                     # read-only: not a write
    open(os.devnull, "w").close()                              # /dev: not a file effect
    open(capsule / "model-calls.jsonl", "a").close()           # NovaFabric's own capsule
    if sys.argv[4] == "os_exit":
        os._exit(0)
""")


def _run_child(tmp_path: Path, how: str) -> tuple[Path, Path, Path]:
    capsule, work, outside = tmp_path / "capsule", tmp_path / "work", tmp_path / "outside"
    for d in (capsule, work, outside):
        d.mkdir()
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(capsule), str(work), str(outside), how],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return capsule, work, outside


def test_the_observer_hands_off_every_path_opened_for_writing(tmp_path: Path) -> None:
    capsule, work, outside = _run_child(tmp_path, "exit")
    handoffs = sorted(p.name for p in capsule.iterdir() if p.name.startswith(HANDOFF_PREFIX))
    assert len(handoffs) == 2 and handoffs[0].endswith(".json")
    assert handoffs[1].endswith(".started")
    doc = json.loads((capsule / handoffs[0]).read_text())
    assert doc["capped"] is False
    assert sorted(doc["paths"]) == sorted(
        str(p) for p in (work / "out.txt", outside / "ledger.txt", work / "p.txt",
                         work / "raw.bin")
    )


def test_a_process_that_skips_atexit_is_seen_as_incomplete(tmp_path: Path) -> None:
    capsule, work, _outside = _run_child(tmp_path, "os_exit")
    record = collect_file_effects(
        capsule, cwd=work, home=tmp_path / "home", exclude=[], digest=True
    )
    assert record is not None
    assert record.complete is False
    assert "ended without handing off" in (record.reason or "")
    assert not [p for p in capsule.iterdir() if p.name.startswith(HANDOFF_PREFIX)]


# ── the orchestrator-side merge ──────────────────────────────────────────────


def _handoff(capsule: Path, pid: int, paths: list[str] | None, *, capped: bool = False,
             raw: bytes | None = None) -> None:
    (capsule / f"{HANDOFF_PREFIX}{pid}.started").write_bytes(b"")
    if paths is not None or raw is not None:
        data = raw if raw is not None else json.dumps({"pid": pid, "paths": paths,
                                                       "capped": capped}).encode()
        (capsule / f"{HANDOFF_PREFIX}{pid}.json").write_bytes(data)


def test_collect_merges_processes_excludes_novafabric_and_digests(tmp_path: Path) -> None:
    capsule, work, home, nova = (tmp_path / n for n in ("capsule", "work", "home", "nova"))
    for d in (capsule, work, home, nova, work / "__pycache__"):
        d.mkdir()
    (work / "a.txt").write_bytes(b"A")
    (home / "h.txt").write_bytes(b"H")
    _handoff(capsule, 1, [str(work / "a.txt"), str(work / "gone.tmp"),
                          str(nova / "audit.jsonl"), str(work / "__pycache__" / "m.pyc")])
    _handoff(capsule, 2, [str(home / "h.txt"), str(work / "a.txt")])
    record = collect_file_effects(capsule, cwd=work, home=home, exclude=[nova], digest=True)
    assert record is not None and record.complete, record
    assert [f.as_dict() for f in record.files] == [
        {"path": "a.txt", "state": "file", "sha256": _sha(b"A"), "size_bytes": 1},
        {"path": "gone.tmp", "state": "absent"},
        {"path": "~/h.txt", "state": "file", "sha256": _sha(b"H"), "size_bytes": 1},
    ]
    assert list(capsule.iterdir()) == []  # every hand-off removed


@pytest.mark.parametrize("raw,needle", [
    (b"{", "not UTF-8 JSON"),
    (json.dumps({"paths": ["relative"], "capped": False}).encode(), "not an absolute path"),
    (json.dumps({"paths": "x", "capped": False}).encode(), "no paths list"),
])
def test_an_invalid_handoff_makes_the_record_incomplete(
    tmp_path: Path, raw: bytes, needle: str
) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    _handoff(capsule, 7, None, raw=raw)
    record = collect_file_effects(capsule, cwd=tmp_path, home=tmp_path, exclude=[], digest=True)
    assert record is not None and not record.complete
    assert needle in (record.reason or "")


def test_a_capped_process_makes_the_record_incomplete(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    _handoff(capsule, 7, [str(tmp_path / "x")], capped=True)
    record = collect_file_effects(capsule, cwd=tmp_path, home=tmp_path, exclude=[], digest=True)
    assert record is not None and not record.complete


def test_a_linked_handoff_is_never_followed(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"paths": [], "capped": False}))
    (capsule / f"{HANDOFF_PREFIX}9.started").write_bytes(b"")
    (capsule / f"{HANDOFF_PREFIX}9.json").symlink_to(target)
    record = collect_file_effects(capsule, cwd=tmp_path, home=tmp_path, exclude=[], digest=True)
    assert record is not None and not record.complete
    assert target.exists()  # the link was removed, never its target


def test_no_observed_process_means_no_record(tmp_path: Path) -> None:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    assert collect_file_effects(
        capsule, cwd=tmp_path, home=tmp_path, exclude=[], digest=True
    ) is None


def test_a_runner_on_another_filesystem_records_nothing(tmp_path: Path) -> None:
    """Under a container or batch runner the paths name another filesystem: no record
    (replay lists file_effects as unchecked), and the hand-offs are still removed."""
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    _handoff(capsule, 3, [str(tmp_path / "x")])
    record = collect_file_effects(capsule, cwd=tmp_path, home=tmp_path, exclude=[],
                                  digest=False)
    assert record is None
    assert list(capsule.iterdir()) == []


# ── file state and paths ─────────────────────────────────────────────────────


def test_file_state_never_reads_a_special_file_or_past_its_bound(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    assert file_state(tmp_path / "fifo", "fifo").state == "not_regular"
    assert file_state(tmp_path, "dir").state == "not_regular"
    assert file_state(Path("/dev/zero"), "z").state == "not_regular"
    (tmp_path / "big").write_bytes(b"x" * 10)
    big = file_state(tmp_path / "big", "big", max_bytes=5)
    assert (big.state, big.size_bytes, big.sha256) == ("too_large", 10, None)
    assert file_state(tmp_path / "missing", "m").state == "absent"


def test_paths_round_trip_against_the_run_directories(tmp_path: Path) -> None:
    cwd, home = tmp_path / "home" / "proj", tmp_path / "home"
    for absolute, shown in (
        (cwd / "out" / "a.json", "out/a.json"),
        (home / ".cache" / "x", "~/.cache/x"),
        (Path("/var/tmp/report.txt"), "/var/tmp/report.txt"),
    ):
        assert display_path(str(absolute), cwd=cwd, home=home) == shown
        assert resolve_recorded_path(shown, cwd=cwd, home=home) == absolute


def test_the_record_round_trips() -> None:
    record = FileEffectsRecord(True, [FileState("a", "file", _sha(b"a"), 1),
                                      FileState("b", "absent")])
    parsed = parse_record(render_record(record).encode())
    assert parsed == record


# ── end to end through `nova capture` ────────────────────────────────────────

_WORKLOAD = textwrap.dedent("""
    import os, sys
    open("result.json", "w").write('{"answer": 42}')
    with open(sys.argv[1], "a") as fh:
        fh.write("secret-free row\\n")
""")


def test_nova_capture_records_paths_and_digests_never_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    work, outside = tmp_path / "work", tmp_path / "outside"
    work.mkdir()
    outside.mkdir()
    (tmp_path / "w.py").write_text(_WORKLOAD)
    monkeypatch.chdir(work)
    result = CliRunner().invoke(app, [
        "capture", "--output-dir", str(tmp_path / "runs"), sys.executable,
        str(tmp_path / "w.py"), str(outside / "ledger.txt"),
    ])
    assert result.exit_code == 0, result.output
    (capsule,) = [p for p in (tmp_path / "runs").iterdir() if (p / "capsule.yaml").exists()]
    raw = (capsule / "file-effects.json").read_bytes()
    record = parse_record(raw)
    assert record.complete, record
    assert {f.path: f.sha256 for f in record.files} == {
        "result.json": _sha(b'{"answer": 42}'),
        str(outside / "ledger.txt"): _sha(b"secret-free row\n"),
    }
    assert b"42" not in raw.replace(_sha(b'{"answer": 42}').encode(), b"")
    assert b"secret-free row" not in raw
    manifest = yaml.safe_load((capsule / "capsule.yaml").read_text())
    assert manifest["evidence_digests"]["file-effects.json"]["sha256"] == _sha(raw)
    assert not [p for p in capsule.iterdir() if p.name.startswith(HANDOFF_PREFIX)]


def test_a_non_python_workload_records_nothing_and_cannot_forge_a_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No process observed: no record -- and a record the workload planted is removed,
    or a replay would compare against it and could read ``faithful``."""
    monkeypatch.chdir(tmp_path)
    forged = json.dumps({"schema": "novafabric.file-effects/1", "complete": True, "files": []})
    script = f"echo x > f.txt; printf '%s' '{forged}' > \"$NOVAFABRIC_CAPSULE_DIR/file-effects.json\""
    result = CliRunner().invoke(
        app, ["capture", "--output-dir", str(tmp_path / "runs"), "sh", "-c", script]
    )
    assert result.exit_code == 0, result.output
    (capsule,) = [p for p in (tmp_path / "runs").iterdir() if (p / "capsule.yaml").exists()]
    assert not (capsule / "file-effects.json").exists()
