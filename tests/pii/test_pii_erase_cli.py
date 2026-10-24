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
"""CLI tests for `nova pii erase` (ADR-0069, cap-001)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.pii.dek import DEKStore

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_store_with_subject(tmp_path: Path, subject_id: str) -> None:
    """Create a DEK for subject_id in the temp NOVAFABRIC_HOME."""
    store = DEKStore(tmp_path / "dek.db")
    store.get_or_create_dek(subject_id)
    store.close()


# ---------------------------------------------------------------------------
# Test 1: nova pii erase exits 0 and writes receipt JSON
# ---------------------------------------------------------------------------


def test_erase_exits_0_and_emits_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nova pii erase <subject_id> exits 0 and outputs receipt JSON."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    subject = "user-test@example.com"
    _setup_store_with_subject(tmp_path, subject)

    result = runner.invoke(
        app,
        ["pii", "erase", subject, "--retention-months", "0"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    # The receipt JSON should appear on stdout.
    # runner captures combined stdout; find the JSON object.
    stdout = result.stdout
    # Find the JSON block (starts with '{').
    json_start = stdout.find("{")
    assert json_start != -1, f"No JSON in output: {stdout!r}"
    receipt_data = json.loads(stdout[json_start:].strip())
    assert receipt_data["subject_id"] == subject


# ---------------------------------------------------------------------------
# Test 2: Receipt contains subject_id, method, legal_basis
# ---------------------------------------------------------------------------


def test_erase_receipt_has_required_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipt JSON contains subject_id, method, and legal_basis fields."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    subject = "fields-check@example.com"
    _setup_store_with_subject(tmp_path, subject)

    result = runner.invoke(
        app,
        ["pii", "erase", subject, "--retention-months", "0"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0

    stdout = result.stdout
    json_start = stdout.find("{")
    receipt_data = json.loads(stdout[json_start:].strip())

    assert receipt_data["subject_id"] == subject
    assert receipt_data["method"] == "aes-256-gcm-dek-destruction"
    assert receipt_data["legal_basis"] == "GDPR Art.17"


# ---------------------------------------------------------------------------
# Test 3: Deferred erasure exits 0 and receipt contains earliest_erasure_at
# ---------------------------------------------------------------------------


def test_deferred_erasure_exits_0_with_earliest_erasure_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deferred erasure exits 0 and the receipt contains earliest_erasure_at."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    subject = "deferred@example.com"
    _setup_store_with_subject(tmp_path, subject)

    # 6-month retention — a brand-new DEK will always be within this window.
    result = runner.invoke(
        app,
        ["pii", "erase", subject, "--retention-months", "6"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, f"stdout={result.stdout}\nstderr={result.stderr}"

    stdout = result.stdout
    json_start = stdout.find("{")
    receipt_data = json.loads(stdout[json_start:].strip())

    assert "earliest_erasure_at" in receipt_data, (
        f"Expected 'earliest_erasure_at' in deferred receipt: {receipt_data}"
    )
    assert receipt_data["subject_id"] == subject
    assert receipt_data["retention_months"] == 6


# ---------------------------------------------------------------------------
# Test 4: --help contains Scope: and Examples:
# ---------------------------------------------------------------------------


def test_erase_help_contains_scope_and_examples() -> None:
    """The erase command's --help text contains 'Scope:' and 'Examples:'."""
    result = runner.invoke(app, ["pii", "erase", "--help"])
    assert result.exit_code == 0
    help_text = result.stdout
    assert "Scope:" in help_text, f"'Scope:' not found in help: {help_text!r}"
    assert "Examples:" in help_text, f"'Examples:' not found in help: {help_text!r}"


# ---------------------------------------------------------------------------
# Review cli-b-09: the receipt names only the subject's capsules; stdout is JSON
# ---------------------------------------------------------------------------

_PEPPER = "test-pepper"


def _capsule_with_subject(capsules: Path, capsule_id: str, subject_id: str) -> None:
    from novafabric.pii.envelope import subject_ref

    cdir = capsules / capsule_id
    cdir.mkdir(parents=True)
    (cdir / "redaction_manifest.json").write_text(json.dumps({
        "capsule_id": capsule_id,
        "entries": [{
            "field_path": "model_calls[0].messages[0].content",
            "detection_rule_id": "EMAIL",
            "legal_basis": "GDPR Art.17",
            "subject_id_hmac": subject_ref(_PEPPER.encode(), subject_id),
            "redacted_at_utc": "2026-10-24T00:00:00Z",
            "protection": "envelope",
        }],
    }))


def _two_subjects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    capsules = tmp_path / "store"
    monkeypatch.setenv("NOVAFABRIC_CAPSULE_DIR", str(capsules))
    _capsule_with_subject(capsules, "capsule-of-alice", "alice@example.com")
    _capsule_with_subject(capsules, "capsule-of-bob", "bob@example.com")
    _setup_store_with_subject(tmp_path, "alice@example.com")
    return capsules


def test_the_receipt_names_only_the_subjects_capsules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every capsule with a redaction manifest was listed as "affected" — bob's too."""
    _two_subjects(tmp_path, monkeypatch)
    monkeypatch.setenv("NOVA_PII_PEPPER", _PEPPER)
    result = runner.invoke(app, ["pii", "erase", "alice@example.com", "--retention-months", "0"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.stdout)  # stdout is the receipt, nothing else
    assert receipt["capsule_ids_affected"] == ["capsule-of-alice"]


def test_without_the_pepper_no_capsule_is_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _two_subjects(tmp_path, monkeypatch)
    monkeypatch.delenv("NOVA_PII_PEPPER", raising=False)
    result = runner.invoke(app, ["pii", "erase", "alice@example.com", "--retention-months", "0"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["capsule_ids_affected"] == []
    assert "NOVA_PII_PEPPER" in result.stderr


def test_stdout_redirected_to_a_file_is_a_valid_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`nova pii erase X > receipt.json` began with "✓ DEK destroyed…"."""
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    _setup_store_with_subject(tmp_path, "carol@example.com")
    result = runner.invoke(app, ["pii", "erase", "carol@example.com", "--retention-months", "0"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["subject_id"] == "carol@example.com"
    assert "DEK destroyed" in result.stderr
