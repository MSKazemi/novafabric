"""``export-examiner bagit`` and ``export-examiner iso42001`` refuse a capsule whose seal fails.

The other exporters (Art. 12, RoPA, PCCP, ``export-compliance iso42001``) already refuse a
capsule that carries a ``.seal`` which no longer verifies. These two still packaged it:
the BagIt archive copied the edited bytes and the broken ``.seal/`` into a bag whose own
checksums verified clean, and the audit-period package listed the capsule as evidence.

Contract pinned here, for the CLI and the dashboard routes alike:

* ``valid`` or ``absent`` seal: the export proceeds exactly as before;
* ``invalid`` seal: :class:`CapsuleSealInvalidError`, CLI exit 2, HTTP 409, and nothing
  is written (no output file, no output directory);
* ``iso42001`` checks the capsules inside the audit period, because those are the ones it
  lists as evidence; a broken seal outside the period does not block the package.

A valid seal means the bytes it names are unchanged since sealing; it says nothing about
who signed them.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from novafabric._capsule_fs import CapsuleLinkError
from novafabric.cli.main import app
from novafabric.compliance.export import examiner
from novafabric.compliance.export._seal_guard import CapsuleSealInvalidError

from .test_seal_invalid_is_refused import (
    DAMAGES,
    _assert_cli_refused,
    _damage,
    _sealed_capsule,
    _unsealed_capsule,
)

runner = CliRunner()

_PERIOD = (
    "2023-01-01/2025-12-31",
    datetime(2023, 1, 1, tzinfo=timezone.utc),
    datetime(2025, 12, 31, tzinfo=timezone.utc),
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "_home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)


def _iso(root: Path, out: Path) -> Path:
    return examiner.ISO42001Exporter(root, _PERIOD[1], _PERIOD[2]).export(out)


def _iso_cli(root: Path, out: Path) -> Any:
    return runner.invoke(
        app,
        [
            "export-examiner",
            "iso42001",
            "--data-dir",
            str(root),
            "--period",
            _PERIOD[0],
            "--output",
            str(out),
        ],
    )


def _bagit_cli(cap: Path, out_dir: Path) -> Any:
    return runner.invoke(
        app,
        [
            "export-examiner",
            "bagit",
            cap.name,
            "--data-dir",
            str(cap),
            "--output-dir",
            str(out_dir),
        ],
    )


def _bag_names(bag: Path) -> list[str]:
    with zipfile.ZipFile(bag) as zf:
        return zf.namelist()


# ---------------------------------------------------------------------------
# bagit
# ---------------------------------------------------------------------------

# BagIt refuses any capsule that holds a symbolic link before it looks at the seal
# (exit 1, CapsuleLinkError), so a symlinked .seal is refused by that older, stricter
# rule and never reaches the seal check. Every other damage reaches it.
_LINKED = {"seal-is-a-symlink", "seal-is-a-dangling-symlink"}
_BAGIT_DAMAGES = [d for d in DAMAGES if d not in _LINKED]


def test_bagit_valid_seal_still_bags_the_capsule(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    bag = examiner.BagItExporter(cap.name, cap).export(tmp_path / "out")
    assert any(n.endswith("/data/.seal/manifest.dsse") for n in _bag_names(bag))


def test_bagit_absent_seal_still_bags_the_capsule(tmp_path: Path) -> None:
    cap = _unsealed_capsule(tmp_path)
    bag = examiner.BagItExporter(cap.name, cap).export(tmp_path / "out")
    assert any(n.endswith("/data/capsule.yaml") for n in _bag_names(bag))


@pytest.mark.parametrize("how", _BAGIT_DAMAGES)
def test_bagit_invalid_seal_is_refused_and_nothing_is_written(tmp_path: Path, how: str) -> None:
    cap = _sealed_capsule(tmp_path)
    _damage(cap, how, tmp_path)
    out_dir = tmp_path / "out"
    with pytest.raises(CapsuleSealInvalidError):
        examiner.BagItExporter(cap.name, cap).export(out_dir)
    assert not out_dir.exists()


def test_bagit_manifest_only_seal_with_an_edited_manifest_is_refused(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path, bind_evidence=False)
    _damage(cap, "edit-manifest", tmp_path)
    with pytest.raises(CapsuleSealInvalidError):
        examiner.BagItExporter(cap.name, cap).export(tmp_path / "out")


def test_bagit_cli_valid_seal_writes_the_bag(tmp_path: Path) -> None:
    cap = _sealed_capsule(tmp_path)
    result = _bagit_cli(cap, tmp_path / "out")
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / f"{cap.name}-bag.zip").exists()


@pytest.mark.parametrize("how", sorted(_LINKED))
def test_bagit_a_symlinked_seal_is_still_refused_by_the_link_rule(tmp_path: Path, how: str) -> None:
    cap = _sealed_capsule(tmp_path)
    _damage(cap, how, tmp_path)
    out_dir = tmp_path / "out"
    with pytest.raises(CapsuleLinkError):
        examiner.BagItExporter(cap.name, cap).export(out_dir)
    assert not out_dir.exists()
    result = _bagit_cli(cap, out_dir)
    assert result.exit_code == 1, result.output
    assert not out_dir.exists()


@pytest.mark.parametrize("how", _BAGIT_DAMAGES)
def test_bagit_cli_invalid_seal_exits_2_with_no_output(tmp_path: Path, how: str) -> None:
    cap = _sealed_capsule(tmp_path)
    _damage(cap, how, tmp_path)
    out_dir = tmp_path / "out"
    result = _bagit_cli(cap, out_dir)
    _assert_cli_refused(result, out_dir, cap)


# ---------------------------------------------------------------------------
# iso42001 (examiner package over an audit period)
# ---------------------------------------------------------------------------


def test_iso42001_valid_and_absent_seals_still_export(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _sealed_capsule(root, "a")
    _unsealed_capsule(root, "b")
    out = _iso(root, tmp_path / "out" / "iso.zip")
    with zipfile.ZipFile(out) as zf:
        assert "capsule-index.json" in zf.namelist()


@pytest.mark.parametrize("how", DAMAGES)
@pytest.mark.parametrize("other", ["valid", "absent"])
def test_iso42001_invalid_seal_in_the_period_is_refused(
    tmp_path: Path, how: str, other: str
) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _damage(_sealed_capsule(root, "broken"), how, tmp_path)
    # a capsule that is fine, or merely unsealed, must not hide the broken one
    (_sealed_capsule if other == "valid" else _unsealed_capsule)(root, "other")
    out = tmp_path / "out" / "iso.zip"
    with pytest.raises(CapsuleSealInvalidError) as err:
        _iso(root, out)
    assert "broken" in str(err.value)
    assert not out.exists()
    assert not out.parent.exists()  # refused before anything was created


def test_iso42001_broken_seal_outside_the_period_does_not_block(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    cap = _sealed_capsule(root, "old")
    _damage(cap, "edit-stream", tmp_path)
    manifest = yaml.safe_load((cap / "capsule.yaml").read_text(encoding="utf-8"))
    manifest["created_at"] = "2019-01-01T00:00:00.000000Z"
    (cap / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    _sealed_capsule(root, "current")
    out = _iso(root, tmp_path / "out" / "iso.zip")
    with zipfile.ZipFile(out) as zf:
        index = zf.read("capsule-index.json").decode("utf-8")
    assert "r-current" in index
    assert "r-old" not in index


def test_iso42001_cli_valid_seal_writes_the_package(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    _sealed_capsule(root, "a")
    out = tmp_path / "iso.zip"
    result = _iso_cli(root, out)
    assert result.exit_code == 0, result.output
    assert out.exists()


@pytest.mark.parametrize("how", DAMAGES)
def test_iso42001_cli_invalid_seal_exits_2_with_no_output(tmp_path: Path, how: str) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    cap = _sealed_capsule(root, "broken")
    _unsealed_capsule(root, "fine")
    _damage(cap, how, tmp_path)
    out = tmp_path / "iso.zip"
    result = _iso_cli(root, out)
    _assert_cli_refused(result, out, cap)
