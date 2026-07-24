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


def test_bare_credentials_in_free_text_are_redacted(tmp_path: Path) -> None:
    """Ruleset v3: a token nothing *names* as a secret must still not leak.

    This is the ADR-0191 D4 side channel — the capsule pipeline strips these
    shapes, so a log or audit line must not carry them out instead.
    """
    from novafabric.support_bundle._redact import redact_line

    # Fixture tokens are assembled at runtime so the SOURCE never contains a
    # contiguous provider-shaped token: GitHub push protection scans the public
    # mirror's source bytes and (correctly) refuses pushes that look like real
    # credentials — the redactor still receives the exact same strings.
    bare = [
        "sk-ant-api03-" + "verysecretvalue0123456789",
        "ghp_" + "seededtokenvalueABCDEFGH",
        "hf_" + "abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIA" + "IOSFODNN7EXAMPLE",
        "xoxb-" + "123456789012-abcdefghijklmnop",
        "nvfk_" + "abcd1234_efghijklmnopqrstuvwxyz0123456789",
    ]
    for secret in bare:
        line = f"2026-07-18 10:00:00 ERROR provider rejected {secret} for run r-1"
        redacted = redact_line(line)
        assert secret not in redacted, f"leak: {line!r} -> {redacted!r}"
        assert "[REDACTED]" in redacted
        # The rest of the line must survive — redaction, not destruction.
        assert "provider rejected" in redacted
        assert "run r-1" in redacted


def test_value_redaction_never_eats_integrity_values(tmp_path: Path) -> None:
    """Over-redaction would break ADR-0191 D5 as surely as under-redaction.

    Entropy-only patterns (bare 64-hex, bare 32/40-char alnum, bare UUID)
    are deliberately excluded from ruleset v3 because they match chain
    hashes, content addresses and run ids that MUST travel intact.
    """
    from novafabric.support_bundle._redact import redact_line

    must_survive = [
        "entry_hash 9f" + "a" * 62,  # 64-hex chain value
        "sha256:" + "b" * 64,  # content address
        "run_id 550e8400-e29b-41d4-a716-446655440000",  # UUID
        "capsule 01JQ8Z9M4KX7VN2P3R5T6Y8W0A",  # ULID
        "duration_ms 1234567890",
    ]
    for line in must_survive:
        assert redact_line(line) == line, f"over-redacted: {line!r}"


def test_value_redaction_covers_every_prefixed_capsule_secret_rule() -> None:
    """Parity guard: what the capsule pack detects, redaction must strip.

    A new prefixed rule in the capsule scanner without a redaction
    counterpart would silently reopen the D4 side channel, so a new rule id
    must be either covered here or explicitly listed as entropy-only.
    """
    from novafabric.capture.secrets import _RULES
    from novafabric.support_bundle._redact import _VALUE_SECRET_PATTERNS

    covered = {rule_id for rule_id, _pattern in _VALUE_SECRET_PATTERNS}
    # Excluded by design — see the _VALUE_SECRET_PATTERNS docstring. These
    # match legitimate hashes/ids, so redacting on them would break D5.
    entropy_only = {
        "cohere-api-key",
        "together-api-key",
        "mistral-api-key",
        "pinecone-api-key",
    }
    for rule in _RULES:
        rule_id = str(rule["id"])
        assert rule_id in covered or rule_id in entropy_only, (
            f"capsule secret rule {rule_id!r} has no redaction counterpart. "
            "Add it to _VALUE_SECRET_PATTERNS in support_bundle/_redact.py, "
            "or to the entropy_only set here with a reason (ADR-0191 D4)."
        )


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
