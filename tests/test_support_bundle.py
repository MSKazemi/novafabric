"""Tests for `nova support-bundle` (ADR-0187, first slice).

Covers the ADR acceptance criteria: allowlist enforcement (deny-by-default),
D2 exclusion classes (seeded secret / env value never appear in the output),
manifest hashes verify and record the ruleset version, and a CLI smoke test.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.support_bundle import (
    ALLOWED_MEMBERS,
    BUNDLE_SCHEMA_VERSION,
    REDACTION_RULESET_VERSION,
    build_support_bundle,
    collect_recent_logs,
)

runner = CliRunner()

FAKE_TOKEN = "sk-fake-supersecret-token-0123456789abcdef"
FAKE_ENV_VALUE = "env-super-secret-value-zz9876"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the developer's real ~/.novafabric data."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "nova-home"))


def _read_members(bundle: Path) -> dict[str, bytes]:
    contents: dict[str, bytes] = {}
    with tarfile.open(bundle, "r:gz") as tar:
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            assert extracted is not None
            contents[member.name] = extracted.read()
    return contents


def test_bundle_created_with_expected_members(tmp_path: Path) -> None:
    out = tmp_path / "bundle.tar.gz"
    result = build_support_bundle(out)
    assert out.exists()
    assert result.path == out
    contents = _read_members(out)
    # No server config in the isolated home -> no config member.
    # No log files in the isolated home -> honest logs/README.txt member.
    assert set(contents) == {
        "doctor.json",
        "versions.json",
        "env.txt",
        "health.json",
        "logs/README.txt",
        "manifest.json",
    }
    assert set(result.members) == set(contents)
    # Members are well-formed JSON where expected.
    doctor = json.loads(contents["doctor.json"])
    assert "storage" in doctor
    versions = json.loads(contents["versions.json"])
    assert versions["novafabric"]
    assert versions["python"]
    assert isinstance(versions["installed_extras"], list)


def test_manifest_hashes_verify_against_members(tmp_path: Path) -> None:
    out = tmp_path / "bundle.tar.gz"
    result = build_support_bundle(out, log_window_hours=6)
    contents = _read_members(out)
    manifest = json.loads(contents["manifest.json"])
    assert manifest["bundle_schema_version"] == BUNDLE_SCHEMA_VERSION
    assert manifest["redaction_ruleset_version"] == REDACTION_RULESET_VERSION
    assert manifest["log_window_hours"] == 6
    assert manifest["created_at"]
    # Every non-manifest member is hashed; hashes and sizes verify.
    assert set(manifest["members"]) == set(contents) - {"manifest.json"}
    for name, entry in manifest["members"].items():
        assert entry["sha256"] == hashlib.sha256(contents[name]).hexdigest()
        assert entry["size_bytes"] == len(contents[name])
    # The reported bundle-level hash is the manifest member's own hash.
    assert result.manifest_sha256 == hashlib.sha256(contents["manifest.json"]).hexdigest()


def test_config_secret_values_never_appear_in_tarball(tmp_path: Path) -> None:
    config = tmp_path / "server.yaml"
    config.write_text(
        "host: 0.0.0.0\n"
        "port: 7433\n"
        f"api_token: {FAKE_TOKEN}\n"
        f"scim_token: {FAKE_TOKEN}\n"
        "database:\n"
        f"  postgres_dsn: postgresql://user:{FAKE_TOKEN}@db/nova\n"
        f"  signing_key: {FAKE_TOKEN}\n"
    )
    out = tmp_path / "bundle.tar.gz"
    build_support_bundle(out, config_path=config)
    contents = _read_members(out)
    assert "config.redacted.yaml" in contents
    # The fake secret must not appear anywhere in the tarball — neither in
    # any member nor in the raw (decompressed or compressed) bytes.
    for name, data in contents.items():
        assert FAKE_TOKEN.encode() not in data, f"secret leaked into {name}"
    assert FAKE_TOKEN.encode() not in out.read_bytes()
    with tarfile.open(out, "r:gz") as tar:
        raw = io.BytesIO()
        for member in tar.getmembers():
            extracted = tar.extractfile(member)
            assert extracted is not None
            raw.write(extracted.read())
        assert FAKE_TOKEN.encode() not in raw.getvalue()
    # Redaction happened and non-secret keys survived.
    redacted = contents["config.redacted.yaml"].decode()
    assert "[REDACTED]" in redacted
    assert "host" in redacted
    assert "0.0.0.0" in redacted


def test_env_values_absent_names_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_FAKE_SETTING", FAKE_ENV_VALUE)
    out = tmp_path / "bundle.tar.gz"
    build_support_bundle(out)
    contents = _read_members(out)
    env_txt = contents["env.txt"].decode()
    assert "NOVAFABRIC_FAKE_SETTING" in env_txt
    for name, data in contents.items():
        assert FAKE_ENV_VALUE.encode() not in data, f"env value leaked into {name}"
    assert FAKE_ENV_VALUE.encode() not in out.read_bytes()


def test_deny_by_default_stray_file_excluded(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "stray.txt").write_text("should never be bundled")
    (staging / "capsule-payload.json").write_text('{"prompt": "secret prompt"}')
    out = tmp_path / "bundle.tar.gz"
    result = build_support_bundle(out, staging_dir=staging)
    contents = _read_members(out)
    assert "stray.txt" not in contents
    assert "capsule-payload.json" not in contents
    assert set(contents) <= set(ALLOWED_MEMBERS)
    assert set(result.members) <= set(ALLOWED_MEMBERS)
    assert b"should never be bundled" not in out.read_bytes()


def test_logs_member_included_and_token_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seeded log file is collected under logs/ with secret values redacted;
    the fake token never appears anywhere in the bundle bytes."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "server.log").write_text(
        "2026-07-16 10:00:00 INFO server started on 127.0.0.1:7433\n"
        f"2026-07-16 10:00:01 DEBUG auth api_token={FAKE_TOKEN} accepted\n"
        f"2026-07-16 10:00:02 INFO postgres_dsn: postgresql://u:{FAKE_TOKEN}@db/nova\n"
    )
    monkeypatch.setenv("NOVAFABRIC_LOG_DIR", str(log_dir))
    out = tmp_path / "bundle.tar.gz"
    build_support_bundle(out)
    contents = _read_members(out)
    assert "logs/server.log" in contents
    assert "logs/README.txt" not in contents
    body = contents["logs/server.log"].decode()
    assert "server started" in body  # non-secret lines survive
    assert "[REDACTED]" in body
    for name, data in contents.items():
        assert FAKE_TOKEN.encode() not in data, f"secret leaked into {name}"
    assert FAKE_TOKEN.encode() not in out.read_bytes()
    # The manifest covers the new member.
    manifest = json.loads(contents["manifest.json"])
    assert "logs/server.log" in manifest["members"]
    entry = manifest["members"]["logs/server.log"]
    assert entry["sha256"] == hashlib.sha256(contents["logs/server.log"]).hexdigest()


def test_logs_absent_dir_yields_honest_readme(tmp_path: Path) -> None:
    """No log dir at all -> the bundle records that honestly, and the
    manifest covers the README member."""
    out = tmp_path / "bundle.tar.gz"
    build_support_bundle(out)  # isolated NOVAFABRIC_HOME has no logs/
    contents = _read_members(out)
    assert "no log files found" in contents["logs/README.txt"].decode()
    assert not any(name.endswith(".log") for name in contents)
    manifest = json.loads(contents["manifest.json"])
    assert "logs/README.txt" in manifest["members"]


def test_collect_recent_logs_tail_truncates_to_max_bytes(tmp_path: Path) -> None:
    lines = [f"line {i:06d} padding-padding-padding" for i in range(500)]
    (tmp_path / "big.log").write_text("\n".join(lines) + "\n")
    members = collect_recent_logs(24, max_bytes=1024, log_dir=tmp_path)
    body = members["logs/big.log"]
    assert body.startswith("[truncated: showing the last 1024 of ")
    assert "line 000499" in body  # the tail is kept ...
    assert "line 000000" not in body  # ... the head is dropped
    # Raw log content honored the byte budget (marker line excluded).
    assert len(body.encode()) <= 1024 + len(body.split("\n", 1)[0]) + 2


def test_collect_recent_logs_window_excludes_old_files(tmp_path: Path) -> None:
    import os as _os
    import time as _time

    old = tmp_path / "old.log"
    old.write_text("ancient entry\n")
    stale = _time.time() - 48 * 3600
    _os.utime(old, (stale, stale))
    fresh = tmp_path / "fresh.log"
    fresh.write_text("recent entry\n")
    members = collect_recent_logs(24, log_dir=tmp_path)
    assert set(members) == {"logs/fresh.log"}
    # Widening the window brings the old file back in.
    members = collect_recent_logs(72, log_dir=tmp_path)
    assert set(members) == {"logs/fresh.log", "logs/old.log"}


def test_collect_recent_logs_readme_when_dir_missing(tmp_path: Path) -> None:
    members = collect_recent_logs(24, log_dir=tmp_path / "nope")
    assert set(members) == {"logs/README.txt"}
    assert "no log files found" in members["logs/README.txt"]


def test_logs_line_redaction_covers_deny_classes(tmp_path: Path) -> None:
    from novafabric.support_bundle._redact import redact_line

    cases = {
        f"api_token={FAKE_TOKEN}": FAKE_TOKEN,
        f"password: {FAKE_TOKEN}": FAKE_TOKEN,
        f'signing_key="{FAKE_TOKEN}"': FAKE_TOKEN,
        f"DSN=postgresql://u:{FAKE_TOKEN}@db/nova": FAKE_TOKEN,
        f"x-credential-id: {FAKE_TOKEN}": FAKE_TOKEN,
        f"client_secret = {FAKE_TOKEN}": FAKE_TOKEN,
    }
    for line, secret in cases.items():
        redacted = redact_line(line)
        assert secret not in redacted, f"leak in {line!r} -> {redacted!r}"
        assert "[REDACTED]" in redacted
    # Non-secret lines pass through unchanged.
    plain = "2026-07-16 10:00:00 INFO capsule abc123 validated"
    assert redact_line(plain) == plain


def test_cli_smoke(tmp_path: Path) -> None:
    out = tmp_path / "cli-bundle.tar.gz"
    result = runner.invoke(app, ["support-bundle", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "manifest.json" in result.output
    assert "Manifest SHA-256" in result.output


def test_cli_default_output_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["support-bundle"])
    assert result.exit_code == 0, result.output
    bundles = list(tmp_path.glob("nova-support-bundle-*.tar.gz"))
    assert len(bundles) == 1
