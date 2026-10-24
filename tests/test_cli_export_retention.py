"""ADR-0159 D5 / NF-277 — the ``nova export-retention`` CLI.

Read-only. Exit 0 on render (``missing`` rows are valid, honest output); exit 2 only on an
unreadable/corrupt bundle or retention source. The honesty banner is always printed.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
from _help_assert import assert_flag_in_help, strip_ansi
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.storage._local_worm import LocalWormAdapter

runner = CliRunner()
TSR = bytes([0x30, 0x05, 0x30, 0x03, 0x02, 0x01, 0x00])


def _bundle(tmp_path: Path, *, tsr: bool) -> Path:
    manifest: dict[str, object] = {"bundle_id": "B1", "subject": {"run_id": "run-1"}}
    if tsr:
        manifest.update(
            timestamp_status="ok",
            timestamp_tsa_url="https://tsa.example",
            manifest_dsse_tsr_sha256="sha256:" + hashlib.sha256(TSR).hexdigest(),
        )
    path = tmp_path / "evidence.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        if tsr:
            zf.writestr("manifest.dsse.tsr", TSR)
    return path


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    reg = tmp_path / ".novafabric" / "registries" / "prod"
    reg.mkdir(parents=True)
    (reg / "retention-policy.yaml").write_text(
        "registry: prod\nretention_days: 2190\ndeletion_mode: prohibited\n"
    )
    LocalWormAdapter(reg / "worm.db").put("run-1", b"capsule", retention_days=30)
    return reg


def _args(tmp_path: Path, bundle: Path, *extra: str) -> list[str]:
    return [
        "export-retention",
        "--bundle",
        str(bundle),
        "--audit-log",
        str(tmp_path / "audit.jsonl"),
        *extra,
    ]


def test_renders_posture_with_timestamp(tmp_path: Path, registry: Path) -> None:
    result = runner.invoke(app, _args(tmp_path, _bundle(tmp_path, tsr=True), "--registry", "prod"))
    assert result.exit_code == 0, result.output
    out = " ".join(strip_ansi(result.output).split())
    assert "17 CFR 240.17a-4" in out
    assert "trusted_timestamp complete" in out
    assert "retention_policy complete" in out
    assert "worm_lock partial" in out  # local adapter is not true WORM
    assert "does not guarantee" in out  # honesty banner


def test_json_mifid_no_registry(tmp_path: Path) -> None:
    result = runner.invoke(
        app, _args(tmp_path, _bundle(tmp_path, tsr=False), "--regime", "mifid", "--json")
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["regime"].startswith("MiFID II")
    rows = {r["element"]: r for r in payload["artifacts"][0]["rows"]}
    assert rows["trusted_timestamp"]["status"] == "missing"
    assert rows["trusted_timestamp"]["reason"]
    assert {r["status"] for r in payload["posture"]} == {"missing"}
    assert "compliant" not in payload and payload["banner"]


def test_corrupt_bundle_exits_two(tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"nope")
    result = runner.invoke(app, _args(tmp_path, bad))
    assert result.exit_code == 2


def test_corrupt_policy_exits_two(tmp_path: Path, registry: Path) -> None:
    (registry / "retention-policy.yaml").write_text("registry: prod\n")
    result = runner.invoke(app, _args(tmp_path, _bundle(tmp_path, tsr=False), "--registry", "prod"))
    assert result.exit_code == 2


def test_help_lists_options() -> None:
    result = runner.invoke(app, ["export-retention", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--bundle")
    assert_flag_in_help(result, "--regime")


def test_oversize_manifest_exits_two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.compliance.export.finance import retention_collect

    monkeypatch.setattr(retention_collect, "_MANIFEST_MAX_BYTES", 8)
    result = runner.invoke(app, _args(tmp_path, _bundle(tmp_path, tsr=False)))
    assert result.exit_code == 2


def test_export_never_writes_worm_db(tmp_path: Path, registry: Path) -> None:
    worm = registry / "worm.db"
    before = (worm.read_bytes(), worm.stat().st_mtime_ns)
    result = runner.invoke(app, _args(tmp_path, _bundle(tmp_path, tsr=True), "--registry", "prod"))
    assert result.exit_code == 0, result.output
    assert (worm.read_bytes(), worm.stat().st_mtime_ns) == before


# --- registry files resolve like ``nova retention`` / the hold store (round 10, B1) ---------


def _policy(reg: Path, deletion_mode: str, days: int = 2190) -> None:
    reg.mkdir(parents=True, exist_ok=True)
    (reg / "retention-policy.yaml").write_text(
        f"registry: prod\nretention_days: {days}\ndeletion_mode: {deletion_mode}\n"
    )


@pytest.fixture
def home_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Policy + WORM lock under ``$NOVAFABRIC_HOME/registries/prod``; cwd holds nothing."""
    home = tmp_path / "home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    reg = home / "registries" / "prod"
    _policy(reg, "prohibited")
    LocalWormAdapter(reg / "worm.db").put("run-1", b"capsule", retention_days=30)
    return reg


def _json(tmp_path: Path, *extra: str) -> dict[str, object]:
    result = runner.invoke(
        app, _args(tmp_path, _bundle(tmp_path, tsr=False), "--registry", "prod", "--json", *extra)
    )
    assert result.exit_code == 0, result.output
    payload: dict[str, object] = json.loads(result.stdout)
    return payload


def _posture(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    return {r["element"]: r for r in payload["posture"]}  # type: ignore[attr-defined,index]


def _worm_row(payload: dict[str, object]) -> dict[str, object]:
    rows = {r["element"]: r for r in payload["artifacts"][0]["rows"]}  # type: ignore[index]
    return rows["worm_lock"]  # type: ignore[no-any-return]


def test_reads_policy_and_worm_db_from_novafabric_home(
    tmp_path: Path, home_registry: Path
) -> None:
    payload = _json(tmp_path)
    policy = _posture(payload)["retention_policy"]
    assert policy["status"] == "complete"
    assert policy["facts"]["deletion_mode"] == "prohibited"  # type: ignore[index]
    assert str(home_registry / "retention-policy.yaml") in policy["source_refs"]  # type: ignore[operator]
    worm = _worm_row(payload)
    assert worm["status"] == "partial"  # local adapter: present, but not true WORM
    assert str(home_registry / "worm.db") in " ".join(worm["source_refs"])  # type: ignore[arg-type]


def test_prohibited_in_any_policy_copy_is_reported(
    tmp_path: Path, home_registry: Path
) -> None:
    _policy(home_registry, "defensible")
    legacy = Path.cwd() / ".novafabric" / "registries" / "prod"
    _policy(legacy, "prohibited", days=30)
    policy = _posture(_json(tmp_path))["retention_policy"]
    assert policy["facts"]["deletion_mode"] == "prohibited"  # type: ignore[index]
    assert policy["facts"]["retention_days"] == "2190"  # type: ignore[index]  # canonical window
    refs = " ".join(policy["source_refs"])  # type: ignore[arg-type]
    assert str(home_registry) in refs and str(legacy) in refs


def test_lock_only_in_legacy_worm_db_is_reported(tmp_path: Path, home_registry: Path) -> None:
    (home_registry / "worm.db").unlink()
    LocalWormAdapter(home_registry / "worm.db").put("other-run", b"x", retention_days=30)
    legacy = Path.cwd() / ".novafabric" / "registries" / "prod"
    legacy.mkdir(parents=True)
    LocalWormAdapter(legacy / "worm.db").put("run-1", b"capsule", retention_days=30)
    worm = _worm_row(_json(tmp_path))
    assert worm["status"] == "partial"
    assert str(legacy / "worm.db") in " ".join(worm["source_refs"])  # type: ignore[arg-type]


def test_corrupt_home_worm_db_exits_two(tmp_path: Path, home_registry: Path) -> None:
    (home_registry / "worm.db").write_bytes(b"not a sqlite database")
    result = runner.invoke(app, _args(tmp_path, _bundle(tmp_path, tsr=False), "--registry", "prod"))
    assert result.exit_code == 2, result.output


def test_explicit_worm_db_is_the_only_one_read(tmp_path: Path, home_registry: Path) -> None:
    other = tmp_path / "other.db"
    LocalWormAdapter(other).put("other-run", b"x", retention_days=30)
    worm = _worm_row(_json(tmp_path, "--worm-db", str(other)))
    assert worm["status"] == "missing"


def test_latest_lock_across_worm_dbs_wins(tmp_path: Path, home_registry: Path) -> None:
    legacy = Path.cwd() / ".novafabric" / "registries" / "prod"
    legacy.mkdir(parents=True)
    LocalWormAdapter(legacy / "worm.db").put("run-1", b"capsule", retention_days=3650)
    worm = _worm_row(_json(tmp_path))
    assert str(legacy / "worm.db") in " ".join(worm["source_refs"])  # type: ignore[arg-type]
    assert worm["facts"]["locked_until"] > "2030"  # type: ignore[index,operator]


def test_dangling_worm_db_link_exits_two(tmp_path: Path, home_registry: Path) -> None:
    (home_registry / "worm.db").unlink()
    (home_registry / "worm.db").symlink_to(tmp_path / "gone.db")
    result = runner.invoke(app, _args(tmp_path, _bundle(tmp_path, tsr=False), "--registry", "prod"))
    assert result.exit_code == 2, result.output
