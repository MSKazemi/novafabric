"""Unit tests for serve/auth.py, serve/audit.py, and serve/__init__.py.

These cover the branches missed by the integration tests and bring
coverage for the experimental dashboard module above the 90 % gate.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")


# ---------------------------------------------------------------------------
# serve/auth.py
# ---------------------------------------------------------------------------


def test_generate_token_is_non_empty() -> None:
    from novafabric.serve.auth import generate_token

    t = generate_token()
    assert len(t) >= 32


def test_write_and_clear_token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.serve import auth

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    token_file = tmp_path / ".serve-token"

    path = auth.write_token_file("test-tok")
    assert path == token_file
    assert token_file.read_text().strip() == "test-tok"

    auth.clear_token_file()
    assert not token_file.exists()

    # clear_token_file is a no-op when the file is already gone
    auth.clear_token_file()


def test_clear_token_file_silences_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.serve import auth

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    token_file = tmp_path / ".serve-token"
    token_file.write_text("tok\n")

    original_unlink = Path.unlink

    def _raise_on_token(self: Path, missing_ok: bool = False) -> None:
        if self == token_file:
            raise OSError("permission denied")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _raise_on_token)
    auth.clear_token_file()  # must not raise


def test_is_localhost_host_accepts_loopback() -> None:
    from novafabric.serve.auth import is_localhost_host

    assert is_localhost_host("127.0.0.1")
    assert is_localhost_host("127.0.0.1:4321")
    assert is_localhost_host("localhost")
    assert is_localhost_host("localhost:8080")
    assert is_localhost_host("[::1]")
    assert is_localhost_host("[::1]:4321")


def test_is_localhost_host_rejects_remote() -> None:
    from novafabric.serve.auth import is_localhost_host

    assert not is_localhost_host("example.com")
    assert not is_localhost_host("10.0.0.1")
    assert not is_localhost_host(None)
    assert not is_localhost_host("")


# ---------------------------------------------------------------------------
# serve/audit.py
# ---------------------------------------------------------------------------


def test_audit_append_and_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))

    from novafabric.serve import audit

    record = audit.append(
        action="test_action",
        args={"key": "value"},
        cli_equivalent="nova test",
        actor_token_fp="abcd1234",
    )
    assert record["action"] == "test_action"
    assert record["result"] == "ok"
    assert "error" not in record

    entries = audit.read_recent(10)
    assert len(entries) == 1
    assert entries[0]["action"] == "test_action"


def test_audit_append_with_error_and_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))

    from novafabric.serve import audit

    record = audit.append(
        action="failing_action",
        args={},
        cli_equivalent="nova fail",
        actor_token_fp="fp",
        result="error",
        error="something went wrong",
        extra={"detail": "more info"},
    )
    assert record["error"] == "something went wrong"
    assert record["extra"] == {"detail": "more info"}


def test_audit_read_recent_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "nofile.jsonl"))

    from novafabric.serve import audit

    assert audit.read_recent() == []


def test_audit_read_recent_skips_blank_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_file = tmp_path / "audit.jsonl"
    audit_file.write_text(
        json.dumps({"action": "a"}) + "\n"
        + "\n"
        + "not-valid-json\n"
        + json.dumps({"action": "b"}) + "\n"
    )
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))

    from novafabric.serve import audit

    entries = audit.read_recent(10)
    assert len(entries) == 2
    assert entries[0]["action"] == "b"  # newest first


def test_audit_read_recent_respects_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_file = tmp_path / "audit.jsonl"
    lines = "\n".join(json.dumps({"action": f"a{i}"}) for i in range(10))
    audit_file.write_text(lines + "\n")
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))

    from novafabric.serve import audit

    assert len(audit.read_recent(3)) == 3


def test_audit_chmod_failure_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(audit_file))

    original_chmod = os.chmod

    def failing_chmod(path: object, mode: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(os, "chmod", failing_chmod)

    from novafabric.serve import audit

    record = audit.append(
        action="chmod_test",
        args={},
        cli_equivalent="nova chmod-test",
        actor_token_fp="fp",
    )
    assert record["action"] == "chmod_test"

    monkeypatch.setattr(os, "chmod", original_chmod)


# ---------------------------------------------------------------------------
# serve/__init__.py — lazy factory
# ---------------------------------------------------------------------------


def test_serve_init_lazy_factory(tmp_path: Path) -> None:
    from novafabric.serve import create_app

    db = tmp_path / "test.db"
    app = create_app(token="tok", capsule_dir=tmp_path, db_path=db)
    assert app is not None


# ---------------------------------------------------------------------------
# serve/capsule_loader.py — error / edge-case paths
# ---------------------------------------------------------------------------


def test_discover_capsule_dirs_missing_base(tmp_path: Path) -> None:
    from novafabric.serve.capsule_loader import discover_capsule_dirs

    assert discover_capsule_dirs(tmp_path / "nonexistent") == []


def test_load_capsule_manifest_missing_file(tmp_path: Path) -> None:
    from novafabric.serve.capsule_loader import load_capsule_manifest

    with pytest.raises(FileNotFoundError):
        load_capsule_manifest(tmp_path / "nodir")


def test_load_jsonl_skips_blank_lines(tmp_path: Path) -> None:
    from novafabric.serve.capsule_loader import load_jsonl

    f = tmp_path / "trace.jsonl"
    f.write_text('{"a":1}\n\n{"b":2}\n')
    result = load_jsonl(tmp_path, "trace.jsonl")
    assert len(result) == 2


def test_list_run_summaries_skips_corrupt_yaml(tmp_path: Path) -> None:
    from novafabric.serve.capsule_loader import list_run_summaries

    bad_run = tmp_path / "bad_run"
    bad_run.mkdir()
    (bad_run / "capsule.yaml").write_text(": : : not valid yaml :")
    result = list_run_summaries(tmp_path)
    assert result == []
