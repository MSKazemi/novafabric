"""The ``file_effects`` fidelity check over synthetic capsules (corpus S6, ADR-0321 amendment).

Every recorded file the replay wrote again must hold, after the replay, the content it
held after the recording. A recorded file the replay never touched is not judged. A
record that is present but unusable (incomplete, redacted, rebound), or replay writes
that were not observed, make the check ``null`` and the verdict ``not_compared``; a
capsule with no record at all lists the check in ``unchecked``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.capture.file_effects import resolve_recorded_path
from novafabric.replay._contract import summarize
from novafabric.replay._fidelity import compute_fidelity, file_effects_check

#: "The replay wrote every recorded file again" (the default of the helpers below).
ALL = object()


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _entry(path: str, data: bytes | None) -> dict[str, Any]:
    if data is None:
        return {"path": path, "state": "absent"}
    return {"path": path, "state": "file", "sha256": _sha(data), "size_bytes": len(data)}


def _capsule(
    tmp_path: Path,
    files: list[dict[str, Any]] | None,
    *,
    complete: bool = True,
    reason: str | None = None,
    raw: bytes | None = None,
    bind: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    capsule = tmp_path / "capsule"
    (capsule / "outputs").mkdir(parents=True)
    (capsule / "outputs" / "stdout.txt").write_bytes(b"hi\n")
    manifest: dict[str, Any] = {
        "run_id": "R1", "capture_mode": "cli-wrapper", "exit_code": 0,
        "evidence_digests": {"outputs/stdout.txt": {"sha256": _sha(b"hi\n"), "size_bytes": 3}},
    }
    if files is not None or raw is not None:
        doc: dict[str, Any] = {
            "schema": "novafabric.file-effects/1", "observer": "python-open-audit-hook",
            "complete": complete, "files": files or [],
        }
        if reason:
            doc["reason"] = reason
        data = raw if raw is not None else json.dumps(doc).encode()
        (capsule / "file-effects.json").write_bytes(data)
        manifest["evidence_digests"]["file-effects.json"] = {
            "sha256": bind or _sha(data), "size_bytes": len(data)
        }
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    return capsule, manifest


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    cwd, home = tmp_path / "work", tmp_path / "home"
    cwd.mkdir()
    home.mkdir()
    return cwd, home


def _recorded_abs(capsule: Path, cwd: Path, home: Path) -> set[str]:
    try:
        doc = json.loads((capsule / "file-effects.json").read_text())
        files = doc["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return set()
    return {str(resolve_recorded_path(f["path"], cwd=cwd, home=home)) for f in files}


def _written(capsule: Path, cwd: Path, home: Path, written: Any) -> set[str]:
    return _recorded_abs(capsule, cwd, home) if written is ALL else set(written)


def _check(capsule: Path, manifest: dict[str, Any], cwd: Path, home: Path,
           proof: dict[str, Any] | None = None, written: Any = ALL) -> dict[str, Any]:
    return file_effects_check(
        capsule, manifest, proof, cwd=cwd, home=home,
        replay_writes=(_written(capsule, cwd, home, written), None),
    ).as_dict()


def _events(paths: set[str] | None, *, capped: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"event": "installed"}]
    if paths is not None:
        out.append({"event": "file_observer_installed", "pid": 1})
        out.append({"event": "files_written", "pid": 1, "paths": sorted(paths),
                    "capped": capped})
    return out


def _verdict(capsule: Path, manifest: dict[str, Any], cwd: Path, home: Path,
             events: list[dict[str, Any]] | None = None) -> Any:
    if events is None:
        events = _events(_recorded_abs(capsule, cwd, home))
    report = summarize([], [], events, divergence_policy="fail", substitute_tools=True)
    return compute_fidelity(
        capsule_dir=capsule, manifest=manifest, model_calls=[], tool_calls=[],
        events=events, report=report, exit_code=0, run_error=None,
        stdout=b"hi\n", stderr=b"", replay_capsule=None, cwd=cwd, home=home,
    )


def test_files_left_as_the_recording_left_them_are_equal(tmp_path: Path) -> None:
    cwd, home = _dirs(tmp_path)
    outside = tmp_path / "outside" / "ledger.txt"
    outside.parent.mkdir()
    (cwd / "out.json").write_bytes(b'{"a": 1}')
    outside.write_bytes(b"row\n")
    (home / "notes.txt").write_bytes(b"n")
    capsule, manifest = _capsule(tmp_path, [
        _entry("out.json", b'{"a": 1}'),
        _entry(str(outside), b"row\n"),
        _entry("~/notes.txt", b"n"),
        _entry("tmp.part", None),  # written, then removed, in both runs
    ])
    check = _check(capsule, manifest, cwd, home)
    assert (check["equal"], check["recorded"], check["replayed"]) == (True, 4, 4), check
    fidelity = _verdict(capsule, manifest, cwd, home)
    assert fidelity.verdict == "faithful", fidelity.as_dict()


def test_a_repeated_append_diverges_and_names_the_file(tmp_path: Path) -> None:
    """Corpus S6 in miniature: the replay appended to the ledger a second time."""
    cwd, home = _dirs(tmp_path)
    ledger = tmp_path / "outside" / "ledger.txt"
    ledger.parent.mkdir()
    ledger.write_bytes(b"row\nrow\n")
    capsule, manifest = _capsule(tmp_path, [_entry(str(ledger), b"row\n")])
    check = _check(capsule, manifest, cwd, home)
    assert check["equal"] is False
    assert check["differences"] == [{
        "path": str(ledger), "recorded": _sha(b"row\n"), "replayed": _sha(b"row\nrow\n"),
    }]
    assert str(ledger) in check["note"]
    fidelity = _verdict(capsule, manifest, cwd, home)
    assert fidelity.verdict == "diverged"
    assert fidelity.failing_checks == ["file_effects"]


@pytest.mark.parametrize("now,expected", [
    (None, "absent"),  # the replay did not produce the output, or deleted it
    (b"other", _sha(b"other")),
])
def test_a_missing_or_changed_output_diverges(
    tmp_path: Path, now: bytes | None, expected: str
) -> None:
    cwd, home = _dirs(tmp_path)
    if now is not None:
        (cwd / "out.json").write_bytes(now)
    capsule, manifest = _capsule(tmp_path, [_entry("out.json", b"recorded")])
    check = _check(capsule, manifest, cwd, home)
    assert check["equal"] is False
    assert check["differences"][0]["replayed"] == expected


def test_a_file_the_recording_removed_that_the_replay_left_diverges(tmp_path: Path) -> None:
    cwd, home = _dirs(tmp_path)
    (cwd / "tmp.part").write_bytes(b"x")
    capsule, manifest = _capsule(tmp_path, [_entry("tmp.part", None)])
    check = _check(capsule, manifest, cwd, home)
    assert check["equal"] is False
    assert check["differences"][0]["recorded"] == "absent"


def test_a_capsule_without_a_record_is_unchecked_not_passed(tmp_path: Path) -> None:
    """A capsule from before file effects were recorded: the check is null and listed in
    ``unchecked`` -- never equal -- and the verdict comes from the other checks."""
    cwd, home = _dirs(tmp_path)
    capsule, manifest = _capsule(tmp_path, None)
    check = _check(capsule, manifest, cwd, home)
    assert check["equal"] is None
    assert "no file-effects.json" in check["note"]
    assert "does not cover" in check["note"]
    fidelity = _verdict(capsule, manifest, cwd, home)
    assert fidelity.verdict == "faithful"
    assert fidelity.uncompared_checks == []
    assert fidelity.as_dict()["unchecked"] == ["file_effects"]


@pytest.mark.parametrize("mutate,needle", [
    ({"complete": False, "reason": "1 observed process(es) ended without handing off"},
     "incomplete"),
    ({"raw": b"not json"}, "not UTF-8 JSON"),
    ({"raw": b'{"schema": "other/9", "complete": true, "files": []}'}, "unknown schema"),
    ({"bind": "sha256:" + "0" * 64}, "does not match its evidence digest"),
])
def test_an_unusable_record_is_not_compared(
    tmp_path: Path, mutate: dict[str, Any], needle: str
) -> None:
    cwd, home = _dirs(tmp_path)
    capsule, manifest = _capsule(tmp_path, [], **mutate)
    check = _check(capsule, manifest, cwd, home)
    assert check["equal"] is None, check
    assert needle in check["note"]
    # Present but unusable is an inconsistency: fail closed, never "unchecked".
    fidelity = _verdict(capsule, manifest, cwd, home)
    assert fidelity.verdict == "not_compared"
    assert "unchecked" not in fidelity.as_dict()


def test_a_record_listed_but_missing_is_not_compared(tmp_path: Path) -> None:
    cwd, home = _dirs(tmp_path)
    capsule, manifest = _capsule(tmp_path, [])
    (capsule / "file-effects.json").unlink()
    check = _check(capsule, manifest, cwd, home)
    assert check["equal"] is None
    assert "listed in evidence_digests but missing" in check["note"]
    assert _verdict(capsule, manifest, cwd, home).verdict == "not_compared"


@pytest.mark.parametrize("key", ["findings", "masker_findings"])
def test_a_redacted_record_is_not_compared(tmp_path: Path, key: str) -> None:
    """A redacted path no longer names the file the workload wrote."""
    cwd, home = _dirs(tmp_path)
    capsule, manifest = _capsule(tmp_path, [_entry("out.json", b"x")])
    (cwd / "out.json").write_bytes(b"x")
    proof = {key: [{"target_ref": "file-effects.json"}]}
    check = _check(capsule, manifest, cwd, home, proof)
    assert check["equal"] is None
    assert "redaction" in check["note"]
    assert file_effects_check(
        capsule, manifest, proof, cwd=cwd, home=home, replay_writes=(set(), None)
    ).unrecorded is False


def test_a_capsule_from_another_capture_mode_is_not_compared(tmp_path: Path) -> None:
    cwd, home = _dirs(tmp_path)
    capsule, manifest = _capsule(tmp_path, [])
    manifest["capture_mode"] = "sdk-decorator"
    assert _check(capsule, manifest, cwd, home)["equal"] is None


def test_a_file_too_large_to_digest_is_not_compared_unless_another_differs(
    tmp_path: Path,
) -> None:
    cwd, home = _dirs(tmp_path)
    big = {"path": "model.bin", "state": "too_large", "size_bytes": 2**31}
    capsule, manifest = _capsule(tmp_path, [big])
    check = _check(capsule, manifest, cwd, home)
    assert check["equal"] is None
    assert "model.bin" in check["note"]
    # A definite difference elsewhere is the stronger, truthful statement.
    capsule2, manifest2 = _capsule(tmp_path / "b", [big, _entry("out.json", b"x")])
    assert _check(capsule2, manifest2, cwd, home)["equal"] is False


def test_a_fifo_at_a_recorded_path_is_never_read(tmp_path: Path) -> None:
    import os

    cwd, home = _dirs(tmp_path)
    os.mkfifo(cwd / "pipe")
    capsule, manifest = _capsule(tmp_path, [_entry("pipe", b"x")])
    check = _check(capsule, manifest, cwd, home)  # returns: O_NONBLOCK, never read
    assert check["equal"] is False
    assert check["differences"][0]["replayed"] == "not_regular"


def test_differences_are_listed_up_to_a_bound(tmp_path: Path) -> None:
    cwd, home = _dirs(tmp_path)
    capsule, manifest = _capsule(tmp_path, [_entry(f"f{i:03}", b"x") for i in range(60)])
    check = _check(capsule, manifest, cwd, home)
    assert check["equal"] is False
    assert len(check["differences"]) == 50
    assert "60 of 60" in check["note"] and "(+10 more)" in check["note"]


# ── only the files the replay wrote again are judged ─────────────────────────


def test_a_recorded_file_the_replay_did_not_touch_is_not_judged(tmp_path: Path) -> None:
    """A served tool wrote it at capture and is (rightly) not run again; it was then
    deleted -- or another run appended to it. The replay did not repeat anything."""
    cwd, home = _dirs(tmp_path)
    (cwd / "side.log").write_bytes(b"something else")
    capsule, manifest = _capsule(tmp_path, [_entry("side.log", b"tool ran\n"),
                                            _entry("out.json", b"x")])
    (cwd / "out.json").write_bytes(b"x")
    check = _check(capsule, manifest, cwd, home, written={str(cwd / "out.json")})
    assert (check["equal"], check["recorded"], check["replayed"]) == (True, 2, 1), check
    assert "1 not touched by the replay (not judged)" in check["note"]
    events = _events({str(cwd / "out.json")})
    assert _verdict(capsule, manifest, cwd, home, events).verdict == "faithful"


def test_files_only_the_replay_wrote_are_counted_not_judged(tmp_path: Path) -> None:
    """The documented gap (fidelity control S6b): a new path each run."""
    cwd, home = _dirs(tmp_path)
    (cwd / "out.json").write_bytes(b"x")
    capsule, manifest = _capsule(tmp_path, [_entry("out.json", b"x")])
    check = _check(capsule, manifest, cwd, home,
                   written={str(cwd / "out.json"), str(cwd / "report-2.txt")})
    assert check["equal"] is True
    assert "1 file(s) written only by the replay (not judged)" in check["note"]


@pytest.mark.parametrize("events,needle", [
    (_events(None), "not observed"),
    ([{"event": "installed"}, {"event": "file_observer_installed", "pid": 7}],
     "ended without reporting"),
    (_events(set(), capped=True), "more files than the observer keeps"),
])
def test_unknown_replay_writes_are_not_compared(
    tmp_path: Path, events: list[dict[str, Any]], needle: str
) -> None:
    cwd, home = _dirs(tmp_path)
    capsule, manifest = _capsule(tmp_path, [_entry("out.json", b"x")])
    fidelity = _verdict(capsule, manifest, cwd, home, events)
    assert fidelity.verdict == "not_compared"
    check = next(c for c in fidelity.as_dict()["checks"] if c["name"] == "file_effects")
    assert check["equal"] is None and needle in check["note"]
    assert "unchecked" not in fidelity.as_dict()


def test_an_empty_record_needs_no_replay_observation(tmp_path: Path) -> None:
    cwd, home = _dirs(tmp_path)
    capsule, manifest = _capsule(tmp_path, [])
    assert _verdict(capsule, manifest, cwd, home, _events(None)).verdict == "faithful"
