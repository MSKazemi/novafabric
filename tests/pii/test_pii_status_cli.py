# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""CLI tests for `nova pii status` (ADR-0069, cap-001)."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.pii.dek import DEKStore

runner = CliRunner()

PEPPER = b"cli-test-pepper"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hmac(subject_id: str) -> str:
    return "sha256:" + hmac.new(PEPPER, subject_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _write_manifest(capsule_dir: Path, capsule_id: str, subject_ids: list[str]) -> None:
    capsule_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {
            "field_path": f"model_calls[{i}].messages[0].content",
            "detection_rule_id": "EMAIL",
            "legal_basis": "GDPR Art.17",
            "subject_id_hmac": _hmac(subject),
            "redacted_at_utc": "2026-01-01T00:00:00+00:00",
        }
        for i, subject in enumerate(subject_ids)
    ]
    (capsule_dir / "redaction_manifest.json").write_text(
        json.dumps({"capsule_id": capsule_id, "entries": entries}),
        encoding="utf-8",
    )


@pytest.fixture()
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    monkeypatch.setenv("NOVA_PII_PEPPER", PEPPER.decode("utf-8"))
    return tmp_path


# ---------------------------------------------------------------------------
# Test 1: status by capsule path exits 0 and reports an active subject
# ---------------------------------------------------------------------------


def test_status_by_path_exits_0_reports_active(home: Path) -> None:
    """nova pii status <capsule_dir> exits 0 and reports the active subject."""
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek("alice@example.com")
    store.close()
    capsule_dir = home / "capsules" / "01HXCLI1"
    _write_manifest(capsule_dir, "01HXCLI1", ["alice@example.com"])

    result = runner.invoke(app, ["pii", "status", str(capsule_dir)], catch_exceptions=False)

    assert result.exit_code == 0, f"stdout={result.stdout}"
    assert "01HXCLI1" in result.stdout
    assert "active" in result.stdout


# ---------------------------------------------------------------------------
# Test 2: --json emits a parseable report
# ---------------------------------------------------------------------------


def test_status_json_output(home: Path) -> None:
    """--json emits a machine-readable PIIStatusReport."""
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek("alice@example.com")
    store.erase_subject("alice@example.com", capsule_ids=[], retention_months=0)
    store.close()
    capsule_dir = home / "capsules" / "01HXCLI2"
    _write_manifest(capsule_dir, "01HXCLI2", ["alice@example.com"])

    result = runner.invoke(
        app,
        ["pii", "status", str(capsule_dir), "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"stdout={result.stdout}"
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["capsule_id"] == "01HXCLI2"
    assert data["encrypted_field_count"] == 1
    assert data["subjects"][0]["dek_state"] == "erased"
    assert data["fields"][0]["detection_rule_id"] == "EMAIL"


# ---------------------------------------------------------------------------
# Test 3: missing capsule exits 1
# ---------------------------------------------------------------------------


def test_status_missing_capsule_exits_1(home: Path) -> None:
    """An unresolvable capsule ID exits 1 with an error message."""
    result = runner.invoke(app, ["pii", "status", "01HXNOPE"])
    assert result.exit_code == 1
    assert "01HXNOPE" in result.output


# ---------------------------------------------------------------------------
# Test 4: no DEK store — still exits 0, reports store absent
# ---------------------------------------------------------------------------


def test_status_without_dek_store_exits_0(home: Path) -> None:
    """Status is read-only: works without dek.db and never creates it."""
    capsule_dir = home / "capsules" / "01HXCLI4"
    _write_manifest(capsule_dir, "01HXCLI4", ["bob@example.com"])

    result = runner.invoke(
        app,
        ["pii", "status", str(capsule_dir), "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["dek_store_present"] is False
    assert data["subjects"][0]["dek_state"] == "erased"
    assert not (home / "dek.db").exists()


# ---------------------------------------------------------------------------
# Test 5: no pepper — dek_state unknown, exit 0
# ---------------------------------------------------------------------------


def test_status_without_pepper_reports_unknown(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without NOVA_PII_PEPPER the CLI still works; correlation is 'unknown'."""
    monkeypatch.delenv("NOVA_PII_PEPPER")
    store = DEKStore(home / "dek.db")
    store.get_or_create_dek("carol@example.com")
    store.close()
    capsule_dir = home / "capsules" / "01HXCLI5"
    _write_manifest(capsule_dir, "01HXCLI5", ["carol@example.com"])

    result = runner.invoke(
        app,
        ["pii", "status", str(capsule_dir), "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["pepper_available"] is False
    assert data["subjects"][0]["dek_state"] == "unknown"


# ---------------------------------------------------------------------------
# Test 6: resolve by capsule ID with --capsule-dir
# ---------------------------------------------------------------------------


def test_status_by_id_with_capsule_dir(home: Path, tmp_path: Path) -> None:
    """A bare capsule ID is resolved under --capsule-dir."""
    other_dir = tmp_path / "elsewhere"
    _write_manifest(other_dir / "run-a", "01HXCLI6", ["dave@example.com"])

    result = runner.invoke(
        app,
        ["pii", "status", "01HXCLI6", "--capsule-dir", str(other_dir), "--json"],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, f"stdout={result.stdout}"
    data = json.loads(result.stdout[result.stdout.find("{") :])
    assert data["capsule_id"] == "01HXCLI6"


# ---------------------------------------------------------------------------
# Test 7: human output — no manifest + no pepper branches
# ---------------------------------------------------------------------------


def test_status_human_output_no_manifest_no_pepper(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human-readable output covers the 'no manifest' and 'no pepper' notices."""
    monkeypatch.delenv("NOVA_PII_PEPPER")
    capsule_dir = home / "capsules" / "01HXCLI7"
    capsule_dir.mkdir(parents=True)

    result = runner.invoke(app, ["pii", "status", str(capsule_dir)], catch_exceptions=False)

    assert result.exit_code == 0
    assert "no PII fields recorded" in result.stdout
    assert "NOVA_PII_PEPPER not set" in result.stdout


# ---------------------------------------------------------------------------
# Test 8: --help contains Scope: and Examples:
# ---------------------------------------------------------------------------


def test_status_help_contains_scope_and_examples() -> None:
    """The status command's --help text contains 'Scope:' and 'Examples:'."""
    result = runner.invoke(app, ["pii", "status", "--help"])
    assert result.exit_code == 0
    assert "Scope:" in result.stdout
    assert "Examples:" in result.stdout
