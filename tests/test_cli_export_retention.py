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
