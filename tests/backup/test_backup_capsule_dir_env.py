"""M12: ``nova backup`` honours ``NOVAFABRIC_CAPSULE_DIR`` like capture does.

The Docker deployment keeps capsules in ``/data/capsules`` (``NOVAFABRIC_CAPSULE_DIR``),
outside ``$NOVAFABRIC_HOME``. A backup that looked only at ``<home>/capsules`` and
``<home>/runs`` recorded ``capsules: absent`` there and silently left every capsule out.
These tests pin that the capsule store capture writes to is in the set, and that restore
puts it back under the same resolution rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric._paths import capsule_dir_for_home, default_capsule_dir
from novafabric.backup import RestoreError, create_backup, restore_backup
from novafabric.cli.main import app

runner = CliRunner()


@pytest.fixture()
def _no_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("novafabric.backup.create.load_signing_profile", lambda: None)


def _home_with_registry(path: Path) -> Path:
    from novafabric.registry.store import get_connection, init_schema

    path.mkdir(parents=True)
    conn = get_connection(path / "registry.db")
    init_schema(conn)
    conn.close()
    return path


@pytest.fixture()
def external_capsules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A capsule store outside the home, named by NOVAFABRIC_CAPSULE_DIR (Docker layout)."""
    capsule_dir = tmp_path / "data" / "capsules"
    (capsule_dir / "run-ext").mkdir(parents=True)
    (capsule_dir / "run-ext" / "capsule.yaml").write_text("run_id: run-ext\n")
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(capsule_dir))
    return capsule_dir


def test_capsule_dir_for_home_is_the_capture_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    assert capsule_dir_for_home(tmp_path) == tmp_path / "capsules"
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(tmp_path / "elsewhere"))
    assert capsule_dir_for_home(tmp_path) == tmp_path / "elsewhere"
    assert default_capsule_dir() == tmp_path / "elsewhere"
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", "  ")
    assert capsule_dir_for_home(tmp_path) == tmp_path / "capsules"


@pytest.mark.usefixtures("_no_signing")
def test_backup_includes_capsules_named_by_env(
    tmp_path: Path, external_capsules: Path
) -> None:
    home = _home_with_registry(tmp_path / "home")
    result = create_backup(tmp_path / "set.tar.gz", home=home)

    by_path = {m.path: m for m in result.manifest.members}
    member = by_path.get("external/capsule-dir/run-ext/capsule.yaml")
    assert member is not None, sorted(by_path)
    assert member.origin == "capsule-dir"
    assert member.role == "capsules"
    status = {c.component: c for c in result.manifest.coverage}
    assert status["capsules"].status == "included"
    assert str(external_capsules) in (status["capsules"].detail or "")


@pytest.mark.usefixtures("_no_signing")
def test_backup_with_default_capsule_dir_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env var: the 0.1.x layout (``capsules/…`` home members) is byte-identical."""
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    home = _home_with_registry(tmp_path / "home")
    (home / "capsules" / "run-1").mkdir(parents=True)
    (home / "capsules" / "run-1" / "capsule.yaml").write_text("run_id: run-1\n")
    result = create_backup(tmp_path / "set.tar.gz", home=home)
    paths = {m.path: m.origin for m in result.manifest.members}
    assert paths["capsules/run-1/capsule.yaml"] == "home"
    assert not any(p.startswith("external/capsule-dir/") for p in paths)


@pytest.mark.usefixtures("_no_signing")
def test_env_pointing_at_home_capsules_is_not_backed_up_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home_with_registry(tmp_path / "home")
    (home / "capsules" / "run-1").mkdir(parents=True)
    (home / "capsules" / "run-1" / "capsule.yaml").write_text("run_id: run-1\n")
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(home / "capsules"))
    result = create_backup(tmp_path / "set.tar.gz", home=home)
    paths = [m.path for m in result.manifest.members]
    assert "capsules/run-1/capsule.yaml" in paths
    assert not any(p.startswith("external/capsule-dir/") for p in paths)


@pytest.mark.usefixtures("_no_signing")
def test_restore_puts_capsules_back_under_the_env_capsule_dir(
    tmp_path: Path, external_capsules: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home_with_registry(tmp_path / "home")
    archive = create_backup(tmp_path / "set.tar.gz", home=home).archive_path

    # Fresh machine, same deployment layout: a new capsule dir named by the env var.
    new_capsules = tmp_path / "new-data" / "capsules"
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(new_capsules))
    result = restore_backup(archive, home=tmp_path / "restored")

    assert result.ok is True, [s for s in result.steps if not s.ok]
    assert (new_capsules / "run-ext" / "capsule.yaml").read_text() == "run_id: run-ext\n"
    assert not (tmp_path / "restored" / "external").exists()


@pytest.mark.usefixtures("_no_signing")
def test_restore_without_env_lands_in_target_home_capsules(
    tmp_path: Path, external_capsules: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home_with_registry(tmp_path / "home")
    archive = create_backup(tmp_path / "set.tar.gz", home=home).archive_path

    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    target = tmp_path / "restored"
    result = restore_backup(archive, home=target)
    assert result.ok is True
    assert (target / "capsules" / "run-ext" / "capsule.yaml").is_file()


@pytest.mark.usefixtures("_no_signing")
def test_restore_refuses_to_overwrite_an_existing_capsule_without_force(
    tmp_path: Path, external_capsules: Path
) -> None:
    home = _home_with_registry(tmp_path / "home")
    archive = create_backup(tmp_path / "set.tar.gz", home=home).archive_path
    # The live capsule store still holds the same capsule: never overwritten silently.
    with pytest.raises(RestoreError, match="already exists"):
        restore_backup(archive, home=tmp_path / "restored")
    result = restore_backup(archive, home=tmp_path / "restored", force=True)
    assert result.ok is True
    assert result.moved_aside is not None
    aside = Path(result.moved_aside) / "external" / "capsule-dir" / "run-ext" / "capsule.yaml"
    assert aside.is_file()


@pytest.mark.usefixtures("_no_signing")
def test_cli_backup_create_reports_env_capsules(
    tmp_path: Path, external_capsules: Path
) -> None:
    home = _home_with_registry(tmp_path / "home")
    out = tmp_path / "set.tar.gz"
    result = runner.invoke(app, ["backup", "create", "-o", str(out), "--home", str(home)])
    assert result.exit_code == 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    from novafabric.backup.verify import read_manifest

    manifest = read_manifest(out)
    assert any(m.origin == "capsule-dir" for m in manifest.members)


@pytest.mark.usefixtures("_no_signing")
def test_a_capsule_dir_member_outside_its_prefix_is_refused(tmp_path: Path) -> None:
    """A crafted manifest cannot use origin ``capsule-dir`` to place an arbitrary file."""
    import hashlib
    import json
    import tarfile

    from novafabric.backup.models import BackupManifest, BackupMember

    data = b"x"
    manifest = BackupManifest(
        set_id="01HZZZZZZZZZZZZZZZZZZZZZZZ",
        created_at="2026-07-16T00:00:00+00:00",
        nova_version="0.0.0-test",
        members=[
            BackupMember(
                path="registry.db",
                sha256=hashlib.sha256(data).hexdigest(),
                size_bytes=1,
                kind="blob",
                origin="capsule-dir",
            )
        ],
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "manifest.json").write_text(json.dumps(manifest.model_dump(mode="json")))
    (work / "m").write_bytes(data)
    archive = tmp_path / "crafted.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(work / "manifest.json", arcname="manifest.json")
        tar.add(work / "m", arcname="registry.db")
    with pytest.raises(RestoreError, match="not under external/capsule-dir"):
        restore_backup(archive, home=tmp_path / "restored")
