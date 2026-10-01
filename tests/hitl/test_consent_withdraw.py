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

"""``nova consent withdraw`` + ``withdraw_recorded_consent`` (ADR-0150 P3 remainder)."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from rich.console import Console
from typer.testing import CliRunner

from novafabric.cli import consent as consent_cli
from novafabric.cli.main import app
from novafabric.hitl import ConsentWithdrawalError, withdraw_recorded_consent

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "conversation"
SCHEMA = REPO_ROOT / "schemas" / "run-capsule.schema.json"
P3 = "p3-accountability-capsule.json"
CID = "consent-0001"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(consent_cli, "console", Console(width=250))


def _doc(fixture: str = P3) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / fixture).read_text())
    return data


def _capsule_dir(tmp_path: Path, data: dict[str, Any]) -> Path:
    capsule = tmp_path / "cap"
    capsule.mkdir(parents=True)
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(data, sort_keys=False))
    return capsule


def _consents(data: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = data["facets"]["conversation"]["consent"]
    return entries


def _withdraw(cap: Path, *extra: str) -> Any:
    return runner.invoke(
        app, ["consent", "withdraw", "--capsule", str(cap), "--consent-id", CID, *extra]
    )


def _squash(text: str) -> str:
    return " ".join(text.replace("│", " ").split())


# ── library ───────────────────────────────────────────────────────────────


def test_library_sets_withdrawn_at_and_keeps_digest() -> None:
    data = _doc()
    before = copy.deepcopy(data)
    out = withdraw_recorded_consent(data, CID, withdrawn_at="2026-09-01T00:00:00Z", now=NOW)
    assert data == before, "input capsule must not be mutated"
    stored = _consents(out.capsule)[out.index]
    assert stored["withdrawn_at"] == "2026-09-01T00:00:00Z"
    assert stored["receipt_digest"] == _consents(before)[0]["receipt_digest"]
    assert out.receipt.withdrawn_at == "2026-09-01T00:00:00Z"
    # Everything outside the one entry is carried over untouched.
    assert out.capsule["facets"]["conversation"]["handoff"] == (
        before["facets"]["conversation"]["handoff"]
    )


@pytest.mark.parametrize(
    ("mutate", "withdrawn_at", "message"),
    [
        (None, "2027-01-01T00:00:00Z", "in the future"),
        (None, "2026-07-14T00:00:00Z", "earlier than given_at"),
        (None, "yesterday", "ISO-8601"),
        (lambda c: c[0].update(withdrawable=False), "2026-09-01T00:00:00Z", "fails its digest"),
        (lambda c: c[0].update(purpose="dpv:Other"), "2026-09-01T00:00:00Z", "fails its digest"),
        (lambda c: c[0].update(withdrawable="yes"), "2026-09-01T00:00:00Z", "malformed"),
        (lambda c: c.append(dict(c[0])), "2026-09-01T00:00:00Z", "recorded 2 times"),
        (lambda c: c[0].update(consent_id="other"), "2026-09-01T00:00:00Z", "no consent receipt"),
    ],
)
def test_library_refusals(mutate: Any, withdrawn_at: str, message: str) -> None:
    data = _doc()
    if mutate is not None:
        mutate(_consents(data))
    with pytest.raises(ConsentWithdrawalError, match=message):
        withdraw_recorded_consent(data, CID, withdrawn_at=withdrawn_at, now=NOW)


def test_library_refuses_capsule_without_consents() -> None:
    with pytest.raises(ConsentWithdrawalError, match="records no consent"):
        withdraw_recorded_consent({"run_id": "r"}, CID, withdrawn_at="2026-09-01T00:00:00Z")


def test_library_refuses_second_withdrawal() -> None:
    first = withdraw_recorded_consent(_doc(), CID, withdrawn_at="2026-09-01T00:00:00Z", now=NOW)
    with pytest.raises(ConsentWithdrawalError, match="already withdrawn"):
        withdraw_recorded_consent(
            first.capsule, CID, withdrawn_at="2026-09-02T00:00:00Z", now=NOW
        )


def test_library_refuses_non_withdrawable_receipt(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc("threaded-capsule.json"))
    recorded = runner.invoke(
        app,
        [
            "consent", "record", "--capsule", str(cap), "--subject", "human:did:example:bob",
            "--purpose", "dpv:ServiceProvision", "--scope", "dpv:Store",
            "--given-at", "2026-07-16T00:00:00Z", "--consent-id", CID, "--not-withdrawable",
        ],
    )
    assert recorded.exit_code == 0, recorded.output
    data = yaml.safe_load((cap / "capsule.yaml").read_text())
    with pytest.raises(ConsentWithdrawalError, match="non-withdrawable"):
        withdraw_recorded_consent(data, CID, withdrawn_at="2026-09-01T00:00:00Z", now=NOW)


# ── CLI ───────────────────────────────────────────────────────────────────


def test_cli_withdraw_writes_valid_capsule_that_still_verifies(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    result = _withdraw(cap, "--withdrawn-at", "2026-09-01T00:00:00Z")
    assert result.exit_code == 0, result.output
    assert "Withdrew consent consent-0001" in result.stdout
    assert "re-issue any seal" in _squash(result.stdout)
    data = yaml.safe_load((cap / "capsule.yaml").read_text())
    jsonschema.validate(data, json.loads(SCHEMA.read_text()))
    assert _consents(data)[0]["withdrawn_at"] == "2026-09-01T00:00:00Z"
    verify = runner.invoke(app, ["consent", "verify", "--capsule", str(cap), "--json"])
    assert verify.exit_code == 0, verify.output
    doc = json.loads(verify.stdout)
    assert doc["status"] == "ok" and doc["verdicts"][0]["withdrawn"] is True
    assert not list(cap.glob(".capsule.yaml.*"))


def test_cli_default_time_is_now(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    result = _withdraw(cap, "--json")
    assert result.exit_code == 0, result.output
    stamp = json.loads(result.stdout)["receipt"]["withdrawn_at"]
    when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - when).total_seconds()) < 120


def test_cli_dry_run_json_does_not_write(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    before = (cap / "capsule.yaml").read_bytes()
    result = _withdraw(cap, "--withdrawn-at", "2026-09-01T00:00:00Z", "--dry-run", "--json")
    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["written"] is False and doc["index"] == 0
    assert doc["receipt"]["withdrawn_at"] == "2026-09-01T00:00:00Z"
    assert "legally valid" in doc["notice"]
    assert (cap / "capsule.yaml").read_bytes() == before
    text = _withdraw(cap, "--dry-run")
    assert "Would withdraw" in text.stdout


def test_cli_second_withdrawal_refused(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    assert _withdraw(cap, "--withdrawn-at", "2026-09-01T00:00:00Z").exit_code == 0
    after_first = (cap / "capsule.yaml").read_bytes()
    again = _withdraw(cap, "--withdrawn-at", "2026-09-02T00:00:00Z")
    assert again.exit_code == 1
    assert "already withdrawn" in _squash(again.output)
    assert (cap / "capsule.yaml").read_bytes() == after_first


def test_cli_unknown_consent_id(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    result = runner.invoke(
        app, ["consent", "withdraw", "--capsule", str(cap), "--consent-id", "nope"]
    )
    assert result.exit_code == 1
    assert "no consent receipt with consent_id" in _squash(result.output)


def test_cli_tampered_receipt_refused(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc("invalid-tampered-consent-capsule.json"))
    ids = [c["consent_id"] for c in _consents(_doc("invalid-tampered-consent-capsule.json"))]
    result = runner.invoke(
        app, ["consent", "withdraw", "--capsule", str(cap), "--consent-id", ids[0]]
    )
    assert result.exit_code == 1
    assert "refusing to withdraw a tampered receipt" in _squash(result.output)


def test_cli_future_time_refused(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    result = _withdraw(cap, "--withdrawn-at", "2999-01-01T00:00:00Z")
    assert result.exit_code == 1
    assert "in the future" in _squash(result.output)


def test_cli_sealed_capsule_refused_unless_forced(tmp_path: Path) -> None:
    cap = _capsule_dir(tmp_path, _doc())
    (cap / ".seal").mkdir()
    before = (cap / "capsule.yaml").read_bytes()
    result = _withdraw(cap, "--withdrawn-at", "2026-09-01T00:00:00Z")
    assert result.exit_code == 1
    assert "--force-unseal" in _squash(result.output)
    assert (cap / "capsule.yaml").read_bytes() == before
    forced = _withdraw(cap, "--withdrawn-at", "2026-09-01T00:00:00Z", "--force-unseal")
    assert forced.exit_code == 0, forced.output


def test_cli_missing_capsule(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["consent", "withdraw", "--capsule", str(tmp_path / "nope"), "--consent-id", CID]
    )
    assert result.exit_code == 1


def test_cli_help() -> None:
    result = runner.invoke(app, ["consent", "withdraw", "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout
