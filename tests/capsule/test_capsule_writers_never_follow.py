"""Writers into an *existing* capsule never write through a planted link (R6 sweep).

A capsule is a plain directory, so anyone who can write into it can replace
``assets.jsonl`` with ``assets.jsonl -> ~/.bashrc``. A writer that then runs
``open(path, "a")`` appends **to the link's target**, as the user running the
command. The readers were hardened earlier (``test_capsule_readers_never_follow``);
these are the writers that open a path inside a capsule after capture finished:

* ``registry.service.record_asset_consumption`` (append to ``assets.jsonl``);
* ``energy._attribution.write_receipts`` (``nova energy attest`` and the SLURM
  runner's sacct receipt, rewrite ``energy-receipts.jsonl``);
* ``capture.schema_validation.write_back_verdicts`` (``nova validate --write``,
  rewrite ``tool-calls.jsonl``).

All go through :func:`novafabric._capsule_fs.open_capsule_file_for_write`. Each
writer refuses with :class:`~novafabric._capsule_fs.CapsuleLinkError`, and the
link's target is byte-for-byte unchanged.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric._capsule_fs import (
    CapsuleLinkError,
    CapsuleSpecialFileError,
    open_capsule_file_for_write,
    open_capsule_member_for_write,
)

CANARY = "do-not-touch\n"


@pytest.fixture
def capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    (cap / "outputs").mkdir(parents=True)
    return cap


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    target = tmp_path / "outside.txt"
    target.write_text(CANARY)
    return target


# ── the helper ───────────────────────────────────────────────────────────────


def test_append_creates_then_appends(capsule: Path) -> None:
    for chunk in (b"one\n", b"two\n"):
        with open_capsule_file_for_write(capsule, "assets.jsonl") as fh:
            fh.write(chunk)
    assert (capsule / "assets.jsonl").read_bytes() == b"one\ntwo\n"


def test_write_truncates(capsule: Path) -> None:
    (capsule / "f.jsonl").write_text("old old old\n")
    with open_capsule_file_for_write(capsule, "f.jsonl", mode="w") as fh:
        fh.write(b"new\n")
    assert (capsule / "f.jsonl").read_text() == "new\n"


def test_exclusive_refuses_an_existing_file(capsule: Path) -> None:
    (capsule / "f.json").write_text("{}")
    with pytest.raises(FileExistsError):
        open_capsule_file_for_write(capsule, "f.json", mode="x")


def test_nested_path_writes_inside_the_capsule(capsule: Path) -> None:
    with open_capsule_file_for_write(capsule, "outputs/answer.txt", mode="w") as fh:
        fh.write(b"42")
    assert (capsule / "outputs" / "answer.txt").read_text() == "42"


@pytest.mark.parametrize("mode", ["a", "w", "x"])
def test_a_linked_file_is_never_written_through(
    capsule: Path, outside: Path, mode: str
) -> None:
    (capsule / "assets.jsonl").symlink_to(outside)
    with pytest.raises(CapsuleLinkError):
        open_capsule_file_for_write(capsule, "assets.jsonl", mode=mode)  # type: ignore[arg-type]
    assert outside.read_text() == CANARY


def test_a_dangling_link_is_never_created_through(capsule: Path, tmp_path: Path) -> None:
    target = tmp_path / "would-be-created.txt"
    (capsule / "assets.jsonl").symlink_to(target)
    with pytest.raises(CapsuleLinkError):
        open_capsule_file_for_write(capsule, "assets.jsonl")
    assert not target.exists()


def test_a_linked_parent_directory_is_never_entered(capsule: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (capsule / "inputs").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(CapsuleLinkError):
        open_capsule_file_for_write(capsule, "inputs/x.txt", mode="w")
    assert list(elsewhere.iterdir()) == []


def test_a_fifo_is_refused_without_blocking(capsule: Path) -> None:
    os.mkfifo(capsule / "assets.jsonl")
    with pytest.raises((CapsuleSpecialFileError, OSError)):
        open_capsule_file_for_write(capsule, "assets.jsonl")


def test_a_linked_capsule_root_is_followed_as_named(capsule: Path, tmp_path: Path) -> None:
    alias = tmp_path / "latest"
    alias.symlink_to(capsule, target_is_directory=True)
    with open_capsule_member_for_write(alias / "assets.jsonl") as fh:
        fh.write(b"x\n")
    assert (capsule / "assets.jsonl").read_text() == "x\n"


def test_unknown_mode_is_a_value_error(capsule: Path) -> None:
    with pytest.raises(ValueError, match="write mode"):
        open_capsule_file_for_write(capsule, "f", mode="r+")  # type: ignore[arg-type]


# ── the writers ──────────────────────────────────────────────────────────────


def test_record_asset_consumption_never_appends_through_a_link(
    capsule: Path, outside: Path
) -> None:
    from novafabric.registry.service import record_asset_consumption

    (capsule / "assets.jsonl").symlink_to(outside)
    with pytest.raises(CapsuleLinkError):
        record_asset_consumption("model@1.0.0", "production", capsule)
    assert outside.read_text() == CANARY


def test_write_receipts_never_writes_through_a_link(capsule: Path, outside: Path) -> None:
    from novafabric.energy._attribution import RECEIPTS_FILE, write_receipts

    (capsule / RECEIPTS_FILE).symlink_to(outside)
    with pytest.raises(CapsuleLinkError):
        write_receipts(capsule, [])
    assert outside.read_text() == CANARY


def test_nova_energy_attest_refuses_a_linked_receipts_file(
    capsule: Path, outside: Path, tmp_path: Path
) -> None:
    from novafabric.cli.main import app
    from novafabric.energy._attribution import RECEIPTS_FILE

    (capsule / "capsule.yaml").write_text("run_id: R1\n")
    (capsule / RECEIPTS_FILE).symlink_to(outside)
    result = CliRunner().invoke(
        app, ["energy", "attest", str(capsule), "--rapl-base", str(tmp_path / "no-rapl")]
    )
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 2, result.output
    assert "symlink" in result.output
    assert outside.read_text() == CANARY


def test_write_back_verdicts_never_rewrites_through_a_link(
    capsule: Path, tmp_path: Path
) -> None:
    from novafabric.capture.schema_validation import write_back_verdicts

    record = json.dumps({"tool_call_id": "t1", "tool_name": "x"}) + "\n"
    outside = tmp_path / "outside.jsonl"
    outside.write_text(record)
    (capsule / "tool-calls.jsonl").symlink_to(outside)
    # The reader refuses the link too, so there is nothing to write back.
    write_back_verdicts(capsule)
    assert outside.read_text() == record


def test_write_back_verdicts_refuses_a_link_planted_after_the_read(
    capsule: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write itself is no-follow, not only the read before it (no TOCTOU)."""
    from novafabric.capture import schema_validation

    (capsule / "tool-calls.jsonl").write_text(
        json.dumps({"tool_call_id": "t1", "tool_name": "x"}) + "\n"
    )
    outside = tmp_path / "outside.jsonl"
    outside.write_text(CANARY)
    real_read = schema_validation.read_capsule_text_or_none

    def read_then_swap(cap: Path, rel: str, **kwargs: object) -> str | None:
        text = real_read(cap, rel, **kwargs)  # type: ignore[arg-type]
        (cap / "tool-calls.jsonl").unlink()
        (cap / "tool-calls.jsonl").symlink_to(outside)
        return text

    monkeypatch.setattr(schema_validation, "read_capsule_text_or_none", read_then_swap)
    # A record that gains a verdict, so the backfill really writes.
    monkeypatch.setattr(
        schema_validation, "compute_verdict", lambda *_a, **_k: {"arguments_valid": True}
    )
    with pytest.raises(CapsuleLinkError):
        schema_validation.write_back_verdicts(capsule)
    assert outside.read_text() == CANARY
