"""A hostile workload cannot redirect, corrupt or hang its own recording.

Under ``nova capture`` the workload writes into its own capsule, and with
``--runner docker`` the capsule directory is bind-mounted read-write into the
container. Host-side finalisation used to follow whatever the workload left
there: a ``model-calls.jsonl -> <outside file>`` link made the secret scanner
rewrite the outside file, a FIFO at ``tool-calls.jsonl`` hung capture forever,
and ``env.lock``, ``capsule.yaml``, ``redaction-proof.json``, ``replay.yaml``,
``capture-health.json`` and ``outputs/stdout.txt`` were all written through
planted links -- a container-to-host write.

Every test here runs a real ``nova capture`` (a subprocess, so a hang is a
timeout, never a stuck test run, and memory is capped) of a small Python
workload that plants the link or FIFO, then asserts: the outside file is
byte-for-byte unchanged, capture terminated with the workload's exit code, and
the capsule records the tamper in ``capture-health.json``,
``redaction-proof.json`` and ``metadata.capture_tampered``.
"""

from __future__ import annotations

import json
import os
import resource
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app

#: A capture that has not finished by now is hung (a clean one takes seconds).
CAPTURE_TIMEOUT_S = 180
#: Address-space cap for the capture subprocess (no unbounded read can take the box).
CAPTURE_MEMORY_BYTES = 3 * 1024**3

_SECRETS = (
    "aws_access_key_id = AKIAIOSFODNN7EXAMPLE\n"
    "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n"
)


def _cap_memory() -> None:  # pragma: no cover - runs in the child
    resource.setrlimit(resource.RLIMIT_AS, (CAPTURE_MEMORY_BYTES, CAPTURE_MEMORY_BYTES))


def _capture(
    tmp_path: Path,
    source: str,
    *args: str,
    capture_args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run ``nova capture`` of *source*; return the process and the one capsule."""
    script = tmp_path / "workload.py"
    script.write_text(textwrap.dedent(source))
    out = tmp_path / "caps"
    env = dict(os.environ)
    for var in ("NOVAFABRIC_CAPSULE_DIR", "NOVA_DATA_DIR", "FORCE_COLOR", "COLORTERM"):
        env.pop(var, None)
    env.update(extra_env or {})
    started = time.monotonic()
    proc = subprocess.run(
        [
            sys.executable, "-m", "novafabric.cli.main", "capture", "--no-daemon",
            "--output-dir", str(out), *capture_args,
            "--", sys.executable, str(script), *args,
        ],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=CAPTURE_TIMEOUT_S,
        preexec_fn=_cap_memory,
    )
    assert time.monotonic() - started < CAPTURE_TIMEOUT_S
    capsules = [p for p in out.iterdir() if p.is_dir() and not p.is_symlink()]
    assert len(capsules) == 1, (capsules, proc.stderr[-2000:])
    return proc, capsules[0]


def _manifest(capsule: Path) -> dict:  # type: ignore[type-arg]
    return yaml.safe_load((capsule / "capsule.yaml").read_text())


def _health(capsule: Path) -> dict:  # type: ignore[type-arg]
    return json.loads((capsule / "capture-health.json").read_text())


def _proof(capsule: Path) -> dict:  # type: ignore[type-arg]
    return json.loads((capsule / "redaction-proof.json").read_text())


def _tampered_paths(capsule: Path) -> set[str]:
    return {e["path"] for e in _health(capsule)["tampered_entries"]}


def _assert_recorded(capsule: Path, path: str, kind: str) -> None:
    """*path* is recorded as tampered (of *kind*) in all three places."""
    entries = {e["path"]: e for e in _health(capsule)["tampered_entries"]}
    assert entries[path]["kind"] == kind, entries
    proof_refs = {t["ref"]: t for t in _proof(capsule)["targets"]}
    target = proof_refs[f"{path}#tampered"]
    assert target["skipped"] is True
    assert target["skip_reason"].startswith("stream_tampered:")
    assert f"{path} ({kind})" in _manifest(capsule)["metadata"]["capture_tampered"]


def _no_links(capsule: Path) -> list[str]:
    return [str(p) for p in capsule.rglob("*") if p.is_symlink()]


# ── hole 1: the secret scanner rewrote a file outside the capsule ───────────


_PLANT_STREAM_LINK = """
    import os, sys
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    p = cap / "model-calls.jsonl"
    p.unlink()
    os.symlink(sys.argv[1], p)
"""


def test_scanner_never_rewrites_a_file_outside_the_capsule(tmp_path: Path) -> None:
    outside = tmp_path / "outside-credentials"
    outside.write_text(_SECRETS)
    before = outside.read_bytes()

    proc, capsule = _capture(tmp_path, _PLANT_STREAM_LINK, str(outside))

    assert outside.read_bytes() == before, "the scanner rewrote a file outside the capsule"
    assert proc.returncode == 0, proc.stderr[-2000:]  # fail-open: the workload's code
    assert "capture integrity" in proc.stderr
    assert not (capsule / "model-calls.jsonl").is_symlink()
    assert (capsule / "model-calls.jsonl").read_bytes() == b""
    _assert_recorded(capsule, "model-calls.jsonl", "symlink")
    assert _manifest(capsule)["model_call_count"] == 0
    assert _no_links(capsule) == []


# ── hole 2: a FIFO at a stream hung capture forever ─────────────────────────


_PLANT_FIFO = """
    import os
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    p = cap / "tool-calls.jsonl"
    p.unlink()
    os.mkfifo(p)
    print("planted fifo")
"""


def test_fifo_at_a_stream_never_hangs_capture(tmp_path: Path) -> None:
    proc, capsule = _capture(tmp_path, _PLANT_FIFO)  # a hang raises TimeoutExpired

    assert proc.returncode == 0, proc.stderr[-2000:]
    _assert_recorded(capsule, "tool-calls.jsonl", "fifo")
    assert (capsule / "tool-calls.jsonl").is_file()
    assert _manifest(capsule)["tool_call_count"] == 0


# ── hole 3: finalize writers wrote through planted links ────────────────────


_PLANTED = [
    "env.lock",
    "replay.yaml",
    "capsule.yaml",
    "redaction-proof.json",
    "capture-health.json",
    "lineage.jsonl",
    "trace.jsonl",
    "outputs/stdout.txt",
    "outputs/stderr.txt",
]

_PLANT_FINALIZE_LINKS = """
    import os, sys
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    canary = Path(sys.argv[1])
    for n in %r:
        tgt = canary / ("canary-" + n.replace("/", "_"))
        tgt.write_text("CANARY-ORIGINAL\\n")
        p = cap / n
        if os.path.lexists(p):
            p.unlink()
        os.symlink(tgt, p)
    os.symlink(canary, cap / ".seal")   # the seal dir, too
    print("workload stdout payload")
    print("workload stderr payload", file=sys.stderr)
    sys.exit(3)
""" % (_PLANTED,)


def test_finalize_never_writes_through_planted_links(tmp_path: Path) -> None:
    canary = tmp_path / "canary"
    canary.mkdir()

    proc, capsule = _capture(tmp_path, _PLANT_FINALIZE_LINKS, str(canary))

    assert proc.returncode == 3, proc.stderr[-2000:]  # the workload's own exit code
    for path in sorted(canary.iterdir()):
        assert path.read_text() == "CANARY-ORIGINAL\n", f"{path.name} was written through"
    assert sorted(p.name for p in canary.iterdir()) == sorted(
        "canary-" + n.replace("/", "_") for n in _PLANTED
    ), "something was created in the outside directory (via the .seal link)"
    assert _no_links(capsule) == []
    assert _tampered_paths(capsule) == {*_PLANTED, ".seal"}
    _assert_recorded(capsule, "capsule.yaml", "symlink")
    _assert_recorded(capsule, ".seal", "symlink")
    # The real evidence was written into the capsule instead.
    assert (capsule / "outputs" / "stdout.txt").read_text() == "workload stdout payload\n"
    assert _manifest(capsule)["exit_code"] == 3
    assert (capsule / "env.lock").is_file() and (capsule / "replay.yaml").is_file()


_PLANT_OUTPUTS_LINK = """
    import os, shutil, sys
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    shutil.rmtree(cap / "outputs")
    os.symlink(sys.argv[1], cap / "outputs")
    print("payload that must not land outside")
"""


def test_a_linked_outputs_directory_is_never_written_into(tmp_path: Path) -> None:
    outside = tmp_path / "outside-dir"
    outside.mkdir()

    proc, capsule = _capture(tmp_path, _PLANT_OUTPUTS_LINK, str(outside))

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert list(outside.iterdir()) == [], "stdout.txt was written outside the capsule"
    _assert_recorded(capsule, "outputs", "symlink")
    assert (capsule / "outputs").is_dir() and not (capsule / "outputs").is_symlink()
    assert (capsule / "outputs" / "stdout.txt").read_text().startswith("payload")


_PLANT_FAKE_SEAL = """
    import os
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    (cap / ".seal").mkdir()
    (cap / ".seal" / "manifest.dsse").write_text("{}")
"""


def test_a_seal_planted_by_the_workload_is_removed_and_recorded(tmp_path: Path) -> None:
    proc, capsule = _capture(tmp_path, _PLANT_FAKE_SEAL)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not (capsule / ".seal").exists(), "a forged .seal/ survived (no signing profile)"
    _assert_recorded(capsule, ".seal", "directory")


_REPLACE_ROOT = """
    import os, sys
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    os.rename(cap, cap.parent / "moved-away")
    os.symlink(sys.argv[1], cap)
"""


def test_a_replaced_capsule_directory_is_never_written_into(tmp_path: Path) -> None:
    outside = tmp_path / "outside-root"
    outside.mkdir()
    script = tmp_path / "workload.py"
    script.write_text(textwrap.dedent(_REPLACE_ROOT))
    env = {k: v for k, v in os.environ.items() if k != "NOVAFABRIC_CAPSULE_DIR"}
    proc = subprocess.run(
        [
            sys.executable, "-m", "novafabric.cli.main", "capture", "--no-daemon",
            "--output-dir", str(tmp_path / "caps"), "--",
            sys.executable, str(script), str(outside),
        ],
        env=env, cwd=tmp_path, capture_output=True, text=True,
        timeout=CAPTURE_TIMEOUT_S, preexec_fn=_cap_memory,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert list(outside.iterdir()) == [], "capture wrote through the replaced capsule path"
    assert "capsule directory was replaced" in proc.stderr


# ── hole 5: a forged capture-health hand-off ────────────────────────────────


_FORGE_HANDOFF = """
    import json, os
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    # Inflated past any sane bound, and a stream name the capsule does not have.
    (cap / ".capture-health.workload.900001.json").write_text(
        json.dumps({"dropped_events": {"model-calls.jsonl": 2**53, "FORGED-stream": 7}}))
    (cap / ".capture-health.workload.900002.json").write_text(
        json.dumps({"dropped_events": {"FORGED-stream": 7}}))
    # A well-formed hand-off is accepted, but labelled as workload-reported.
    (cap / ".capture-health.workload.900003.json").write_text(
        json.dumps({"dropped_events": {"tool-calls.jsonl": 2}}))
"""


def test_a_forged_health_hand_off_cannot_inflate_or_invent_streams(tmp_path: Path) -> None:
    proc, capsule = _capture(tmp_path, _FORGE_HANDOFF)

    assert proc.returncode == 0, proc.stderr[-2000:]
    health = _health(capsule)
    assert health["dropped_events"] == {"tool-calls.jsonl": 2}
    assert health["sources"] == {"workload": {"tool-calls.jsonl": 2}}
    assert "not verified" in health["sources_note"]
    assert not list(capsule.glob(".capture-health.workload.*"))


# ── verify and validate fail a tampered capsule ─────────────────────────────


def test_nova_validate_and_verify_fail_a_tampered_capsule(tmp_path: Path) -> None:
    runner = CliRunner()
    home = Path(os.environ["NOVAFABRIC_HOME"])
    init = runner.invoke(app, ["seal", "init", "--home", str(home)])
    assert init.exit_code == 0, init.output
    outside = tmp_path / "outside"
    outside.write_text("x\n")

    proc, capsule = _capture(tmp_path, _PLANT_STREAM_LINK, str(outside))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert (capsule / ".seal" / "manifest.dsse").is_file(), "the tamper record is sealed"

    validated = runner.invoke(app, ["validate", str(capsule)])
    assert validated.exit_code == 1, validated.output
    assert "capture recorded tampering" in validated.output

    verified = runner.invoke(app, ["verify", "--json", str(capsule)])
    assert verified.exit_code == 1, verified.output
    data = json.loads(verified.output)
    assert data["valid"] is False
    assert data["capsule_binding_ok"] is True  # the seal signs the tamper record
    assert "model-calls.jsonl (symlink)" in data["capture_tampered"]
    assert any("capture recorded tampering" in e for e in data["errors"])


# ── a clean workload is unaffected ──────────────────────────────────────────


def test_a_clean_workload_records_no_tamper(tmp_path: Path) -> None:
    runner = CliRunner()
    proc, capsule = _capture(tmp_path, "print('clean')\n")

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "capture integrity" not in proc.stderr
    assert not (capsule / "capture-health.json").exists()
    assert "metadata" not in _manifest(capsule)
    assert not [t for t in _proof(capsule)["targets"] if t["ref"].endswith("#tampered")]
    assert (capsule / "outputs" / "stdout.txt").read_text() == "clean\n"
    validated = runner.invoke(app, ["validate", str(capsule)])
    assert validated.exit_code == 0, validated.output


def test_streams_module_covers_every_scanned_stream() -> None:
    from novafabric.capture.secrets import SCAN_TARGETS
    from novafabric.capture.streams import CAPSULE_STREAMS

    scanned = {n for n, _kind in SCAN_TARGETS if n.endswith(".jsonl")}
    assert scanned <= CAPSULE_STREAMS
    assert {"assets.jsonl", "tool-permission-events.jsonl"} <= CAPSULE_STREAMS


# ── residual holes (attack round 4) ─────────────────────────────────────────


def _special_entries(capsule: Path) -> list[str]:
    """Every entry under *capsule* that is neither a directory nor a regular file."""
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(capsule, followlinks=False):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            mode = path.lstat().st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                found.append(path.relative_to(capsule).as_posix())
    return sorted(found)


def _nova(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a ``nova`` sub-command as a bounded subprocess (a FIFO can never hang it)."""
    env = {k: v for k, v in os.environ.items() if k not in ("FORCE_COLOR", "COLORTERM")}
    env.pop("NOVAFABRIC_CAPSULE_DIR", None)
    env["COLUMNS"] = "400"  # keep long paths on one line in the rich output
    return subprocess.run(
        [sys.executable, "-m", "novafabric.cli.main", *args],
        env=env, capture_output=True, text=True,
        timeout=CAPTURE_TIMEOUT_S, preexec_fn=_cap_memory,
    )


_PII_SUBJECT = "alice@example.com"

_PLANT_PII_LINKS = """
    import os, sys
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    outside = Path(sys.argv[1])
    os.symlink(outside / "victim_manifest.txt", cap / "redaction_manifest.json")
    os.link(outside / "victim_hard.txt", cap / "outputs" / "notes.txt")
    print("subject alice@example.com email alice@example.com")
"""


def test_pii_stage_never_writes_outside_the_capsule(tmp_path: Path) -> None:
    """Hole 1: ``--pii-subject`` wrote its manifest through a link and rewrote a hard link."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "victim_manifest.txt").write_text("VICTIM-MANIFEST-ORIGINAL\n")
    (outside / "victim_hard.txt").write_text(f"contact {_PII_SUBJECT} about the bill\n")
    before = {p.name: p.read_bytes() for p in outside.iterdir()}

    proc, capsule = _capture(
        tmp_path, _PLANT_PII_LINKS, str(outside),
        capture_args=("--pii-subject", _PII_SUBJECT),
        extra_env={"NOVA_PII_PEPPER": "pepper-for-tests"},
    )

    assert proc.returncode == 0, proc.stderr[-2000:]
    after = {p.name: p.read_bytes() for p in outside.iterdir()}
    assert after == before, "the PII stage wrote outside the capsule"
    assert _special_entries(capsule) == []
    _assert_recorded(capsule, "redaction_manifest.json", "symlink")
    # The stage's own manifest is a regular file inside the capsule.
    manifest = json.loads((capsule / "redaction_manifest.json").read_text())
    assert manifest["entries"], manifest
    # The hard-linked file was protected by replacing it, never written through.
    notes = capsule / "outputs" / "notes.txt"
    assert notes.stat().st_nlink == 1
    assert _PII_SUBJECT not in notes.read_text()
    assert "outputs/notes.txt" not in _tampered_paths(capsule)  # a hard link is not tampering


_PLANT_NESTED = """
    import os, sys
    from pathlib import Path
    cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
    outside = Path(sys.argv[1])
    os.symlink(outside / "secret.txt", cap / "inputs" / "data.csv")
    (cap / "outputs" / "media").mkdir(parents=True, exist_ok=True)
    os.symlink(outside / "secret.txt", cap / "outputs" / "media" / "blob.bin")
    (cap / "extensions" / "deep").mkdir(parents=True)
    os.symlink(outside / "secret.txt", cap / "extensions" / "deep" / "ext.json")
    os.symlink(outside, cap / "outputs" / "dirlink")
    os.mkfifo(cap / "inputs" / "pipe")
    os.link(outside / "hard.txt", cap / "inputs" / "hard.txt")
    os.symlink(outside / "secret.txt", cap / "scores.jsonl")
    os.symlink(outside / "secret.txt", cap / "trace.extra.jsonl")
    print("planted")
"""

_NESTED_PLANTED = {
    "inputs/data.csv": "symlink",
    "outputs/media/blob.bin": "symlink",
    "extensions/deep/ext.json": "symlink",
    "outputs/dirlink": "symlink",
    "inputs/pipe": "fifo",
    "scores.jsonl": "symlink",
    "trace.extra.jsonl": "symlink",
}


def test_nested_links_and_fifos_are_swept_and_recorded(tmp_path: Path) -> None:
    """Hole 2a: the sweep never descended, so nested links and FIFOs survived."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET " + _SECRETS)
    (outside / "hard.txt").write_text(_SECRETS)
    before = {p.name: p.read_bytes() for p in outside.iterdir()}

    proc, capsule = _capture(tmp_path, _PLANT_NESTED, str(outside))

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert {p.name: p.read_bytes() for p in outside.iterdir()} == before
    assert _special_entries(capsule) == [], "a nested link or FIFO survived the sweep"
    for path, kind in _NESTED_PLANTED.items():
        _assert_recorded(capsule, path, kind)
    # A hard link is a regular file: kept, scanned and redacted by replacement
    # (the outside file is unchanged, above), and not recorded as tampering.
    hard = capsule / "inputs" / "hard.txt"
    assert hard.stat().st_nlink == 1
    assert "AKIAIOSFODNN7EXAMPLE" not in hard.read_text()
    assert "inputs/hard.txt" not in _tampered_paths(capsule)
    validated = _nova("validate", str(capsule))
    assert validated.returncode == 1, validated.stdout


def test_validate_and_verify_fail_a_link_planted_after_sealing(tmp_path: Path) -> None:
    """Hole 2b: a link or FIFO added to a sealed capsule left verify and validate green."""
    home = Path(os.environ["NOVAFABRIC_HOME"])
    init = _nova("seal", "init", "--home", str(home))
    assert init.returncode == 0, init.stdout + init.stderr
    outside = tmp_path / "outside-secret"
    outside.write_text("TOPSECRET\n")
    proc, capsule = _capture(tmp_path, "print('clean')\n")
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _nova("validate", str(capsule)).returncode == 0
    clean = _nova("verify", "--json", str(capsule))
    assert clean.returncode == 0, clean.stdout + clean.stderr

    (capsule / "inputs" / "nested").mkdir()
    os.symlink(outside, capsule / "inputs" / "nested" / "data.csv")
    os.mkfifo(capsule / "outputs" / "pipe")

    validated = _nova("validate", str(capsule))
    assert validated.returncode == 1, validated.stdout
    assert "inputs/nested/data.csv" in validated.stdout
    assert "outputs/pipe" in validated.stdout
    verified = _nova("verify", "--json", str(capsule))
    assert verified.returncode == 1, verified.stdout
    data = json.loads(verified.stdout)
    assert data["valid"] is False
    assert any("inputs/nested/data.csv" in e and "symlink" in e for e in data["errors"])
    assert any("outputs/pipe" in e and "fifo" in e for e in data["errors"])
    assert outside.read_text() == "TOPSECRET\n"


def test_a_clean_capture_has_no_special_entries(tmp_path: Path) -> None:
    proc, capsule = _capture(
        tmp_path,
        """
        import os
        from pathlib import Path
        cap = Path(os.environ["NOVAFABRIC_CAPSULE_DIR"])
        (cap / "inputs" / "a" / "b").mkdir(parents=True)
        (cap / "inputs" / "a" / "b" / "data.txt").write_text("plain\\n")
        (cap / "outputs" / "notes.txt").write_text("notes\\n")
        print("clean")
        """,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "capture integrity" not in proc.stderr
    assert _special_entries(capsule) == []
    assert "metadata" not in _manifest(capsule)
    assert (capsule / "inputs" / "a" / "b" / "data.txt").read_text() == "plain\n"
