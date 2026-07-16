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
"""CLI tests for `nova retention` (ADR-0134)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.storage._local_worm import LocalWormAdapter

runner = CliRunner()

REGISTRY = "test-reg"
NOW = datetime.now(tz=timezone.utc)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated cwd + NOVAFABRIC_HOME with one registry, policy, and capsules."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        "novafabric.cli.retention.AUDIT_LOG_PATH", tmp_path / "audit.jsonl"
    )
    reg_dir = tmp_path / ".novafabric" / "registries" / REGISTRY
    reg_dir.mkdir(parents=True)
    (reg_dir / "retention-policy.yaml").write_text(
        """
version: 1
registry: test-reg
retention_days: 30
deletion_mode: defensible
bindings:
  - id: old-trades-purge
    description: purge old trade capsules
    match:
      tag: trade
    window: P90D
    action: purge
"""
    )
    capsules = tmp_path / "home" / "capsules"
    _write_capsule(capsules, "cap-old", NOW - timedelta(days=400), tags="trade")
    _write_capsule(capsules, "cap-fresh", NOW - timedelta(days=1), tags="trade")
    _write_capsule(capsules, "cap-other", NOW - timedelta(days=400), tags="other")
    return tmp_path


def _write_capsule(capsules: Path, run_id: str, created_at: datetime, tags: str) -> None:
    d = capsules / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "capsule.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "created_at": created_at.isoformat(),
                "metadata": {"tags": tags},
            }
        )
    )


def _capsule_dir(workspace: Path, run_id: str) -> Path:
    return workspace / "home" / "capsules" / run_id


# ---------------------------------------------------------------------------
# plan (dry-run default): lists candidates, touches nothing
# ---------------------------------------------------------------------------


def test_plan_lists_due_candidates_and_touches_nothing(workspace: Path) -> None:
    result = runner.invoke(
        app, ["retention", "plan", "--registry", REGISTRY, "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "dry-run"
    assert [d["item_id"] for d in payload["due"]] == ["cap-old"]
    assert payload["due"][0]["action"] == "purge"
    assert payload["due"][0]["outcome"] == "dry-run"
    assert payload["held"] == []
    # nothing touched, no audit written
    for cap in ("cap-old", "cap-fresh", "cap-other"):
        assert _capsule_dir(workspace, cap).exists()
    assert not (workspace / "audit.jsonl").exists()


def test_plan_with_no_bindings_sweeps_nothing(workspace: Path) -> None:
    reg_dir = workspace / ".novafabric" / "registries" / "empty-reg"
    reg_dir.mkdir(parents=True)
    result = runner.invoke(
        app, ["retention", "plan", "--registry", "empty-reg", "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["due"] == [] and payload["held"] == []


# ---------------------------------------------------------------------------
# apply: confirm-gated, applies exactly the plan, writes audit
# ---------------------------------------------------------------------------


def test_apply_without_yes_prompts_and_aborts(workspace: Path) -> None:
    result = runner.invoke(
        app, ["retention", "apply", "--registry", REGISTRY], input="n\n"
    )
    assert result.exit_code == 1
    assert "Aborted" in result.output
    assert _capsule_dir(workspace, "cap-old").exists()  # nothing deleted
    assert not (workspace / "audit.jsonl").exists()


def test_apply_confirmed_interactively_applies(workspace: Path) -> None:
    result = runner.invoke(
        app, ["retention", "apply", "--registry", REGISTRY], input="y\n"
    )
    assert result.exit_code == 0, result.output
    assert not _capsule_dir(workspace, "cap-old").exists()


def test_apply_yes_purges_exactly_the_plan_and_writes_audit(workspace: Path) -> None:
    result = runner.invoke(
        app,
        ["retention", "apply", "--registry", REGISTRY, "--yes", "--principal", "cron://t"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert not _capsule_dir(workspace, "cap-old").exists()  # exactly the plan
    assert _capsule_dir(workspace, "cap-fresh").exists()
    assert _capsule_dir(workspace, "cap-other").exists()
    audit_lines = (workspace / "audit.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1
    entry = json.loads(audit_lines[0])
    assert entry["event_type"] == "retention.action"
    assert entry["actor"] == "cron://t"
    assert entry["resource_id"] == "cap-old"
    assert entry["details"]["outcome"] == "applied"
    assert entry["details"]["binding_id"] == "old-trades-purge"


def test_apply_dry_run_touches_nothing(workspace: Path) -> None:
    result = runner.invoke(
        app,
        ["retention", "apply", "--registry", REGISTRY, "--dry-run", "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert json.loads(result.output)["mode"] == "dry-run"
    assert _capsule_dir(workspace, "cap-old").exists()
    assert not (workspace / "audit.jsonl").exists()


def test_apply_worm_locked_capsule_is_skipped_not_purged(workspace: Path) -> None:
    """WORM-holds always win: the locked capsule survives the apply."""
    worm_db = workspace / ".novafabric" / "registries" / REGISTRY / "worm.db"
    adapter = LocalWormAdapter(worm_db)
    adapter.put("cap-old", b"data", retention_days=3650)

    result = runner.invoke(
        app,
        ["retention", "apply", "--registry", REGISTRY, "--yes", "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["due"] == []
    assert [h["item_id"] for h in payload["held"]] == ["cap-old"]
    assert payload["held"][0]["reason"] == "worm_hold"
    assert payload["held"][0]["outcome"] == "skipped"
    assert _capsule_dir(workspace, "cap-old").exists()  # NEVER purged under lock
    # skip recorded as evidence
    entry = json.loads((workspace / "audit.jsonl").read_text().splitlines()[0])
    assert entry["details"]["reason"] == "worm_hold"


def test_apply_legal_hold_blocks_everything(workspace: Path) -> None:
    holds_path = workspace / ".novafabric" / "registries" / REGISTRY / "holds.jsonl"
    holds_path.write_text(
        json.dumps(
            {
                "hold_id": "lit-1",
                "registry": REGISTRY,
                "reason": "litigation",
                "duration_days": None,
                "created_at": NOW.isoformat(),
                "released_at": None,
            }
        )
        + "\n"
    )
    result = runner.invoke(
        app,
        ["retention", "apply", "--registry", REGISTRY, "--yes", "--json"],
        catch_exceptions=False,
    )
    payload = json.loads(result.output)
    assert payload["held"][0]["reason"] == "legal_hold_active"
    assert _capsule_dir(workspace, "cap-old").exists()


def test_apply_action_filter_and_unknown_action(workspace: Path) -> None:
    ok = runner.invoke(
        app,
        ["retention", "apply", "--registry", REGISTRY, "--yes", "--json",
         "--action", "crypto-shred"],
        catch_exceptions=False,
    )
    assert ok.exit_code == 0
    payload = json.loads(ok.output)
    assert payload["due"] == []  # the only binding is a purge binding
    assert _capsule_dir(workspace, "cap-old").exists()

    bad = runner.invoke(
        app, ["retention", "apply", "--registry", REGISTRY, "--action", "explode"]
    )
    assert bad.exit_code == 1
    assert "unknown action" in bad.output


def test_apply_malformed_binding_fails_closed(workspace: Path) -> None:
    policy = workspace / ".novafabric" / "registries" / REGISTRY / "retention-policy.yaml"
    policy.write_text("bindings:\n  - id: bad\n    match: {}\n    window: P1D\n    action: purge\n")
    result = runner.invoke(app, ["retention", "apply", "--registry", REGISTRY, "--yes"])
    assert result.exit_code == 1
    assert _capsule_dir(workspace, "cap-old").exists()


# ---------------------------------------------------------------------------
# status / explain (read-only)
# ---------------------------------------------------------------------------


def test_status_reports_due_held_and_next_due(workspace: Path) -> None:
    worm_db = workspace / ".novafabric" / "registries" / REGISTRY / "worm.db"
    LocalWormAdapter(worm_db).put("cap-old", b"data", retention_days=3650)
    result = runner.invoke(
        app, ["retention", "status", "--registry", REGISTRY, "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    row = payload["bindings"][0]
    assert row["binding_id"] == "old-trades-purge"
    assert row["due_now"] == 0
    assert row["held"] == 1
    assert row["next_due_at"] is not None  # cap-fresh due in ~89 days
    # read-only
    assert _capsule_dir(workspace, "cap-old").exists()
    assert not (workspace / "audit.jsonl").exists()


def test_explain_shows_bindings_due_date_and_hold_state(workspace: Path) -> None:
    worm_db = workspace / ".novafabric" / "registries" / REGISTRY / "worm.db"
    LocalWormAdapter(worm_db).put("cap-old", b"data", retention_days=3650)
    result = runner.invoke(
        app,
        ["retention", "explain", "cap-old", "--registry", REGISTRY, "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["item_id"] == "cap-old"
    assert payload["worm_locked_until"] is not None
    assert payload["matched_bindings"][0]["binding_id"] == "old-trades-purge"
    assert payload["matched_bindings"][0]["due_now"] is True

    missing = runner.invoke(
        app, ["retention", "explain", "no-such-capsule", "--registry", REGISTRY]
    )
    assert missing.exit_code == 1


def test_explain_unmatched_capsule_is_never_swept(workspace: Path) -> None:
    result = runner.invoke(
        app,
        ["retention", "explain", "cap-other", "--registry", REGISTRY, "--json"],
        catch_exceptions=False,
    )
    assert json.loads(result.output)["matched_bindings"] == []


# ---------------------------------------------------------------------------
# help smoke
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("args", [["retention", "--help"], ["retention", "apply", "--help"]])
def test_help_smoke(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 0
    assert "retention" in result.output.lower()


# ---------------------------------------------------------------------------
# Human-readable output paths and end-to-end crypto-shred via CLI
# ---------------------------------------------------------------------------


def test_plan_human_output_table_and_nothing_due(workspace: Path) -> None:
    result = runner.invoke(
        app, ["retention", "plan", "--registry", REGISTRY], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "cap-old" in result.output
    assert "old-trades-purge" in result.output

    # empty registry -> "Nothing due"
    (workspace / ".novafabric" / "registries" / "empty2").mkdir(parents=True)
    empty = runner.invoke(
        app, ["retention", "plan", "--registry", "empty2"], catch_exceptions=False
    )
    assert "Nothing due" in empty.output


def test_apply_yes_human_summary(workspace: Path) -> None:
    result = runner.invoke(
        app, ["retention", "apply", "--registry", REGISTRY, "--yes"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "Sweep complete" in result.output
    assert "1 applied" in result.output


def test_apply_crypto_shred_end_to_end_reuses_dek_store(workspace: Path) -> None:
    """The CLI shred path opens $NOVAFABRIC_HOME/dek.db — same store as nova pii erase."""
    from novafabric.pii.dek import open_dek_store

    home = workspace / "home"
    store = open_dek_store(home)
    store.get_or_create_dek("user@example.com")
    store.close()

    capsules = home / "capsules"
    d = capsules / "cap-pii"
    d.mkdir(parents=True)
    (d / "capsule.json").write_text(
        json.dumps(
            {
                "run_id": "cap-pii",
                "created_at": (NOW - timedelta(days=400)).isoformat(),
                "metadata": {"tags": "contains-pii", "pii_subject_id": "user@example.com"},
            }
        )
    )
    policy = workspace / ".novafabric" / "registries" / REGISTRY / "retention-policy.yaml"
    policy.write_text(
        policy.read_text()
        + "  - id: gdpr-shred\n"
        + "    match:\n      tag: contains-pii\n"
        + "    window: P180D\n    action: crypto-shred\n"
    )

    result = runner.invoke(
        app,
        ["retention", "apply", "--registry", REGISTRY, "--yes", "--json",
         "--action", "crypto-shred", "--retention-months", "0"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [r["item_id"] for r in payload["due"]] == ["cap-pii"]
    assert payload["due"][0]["outcome"] == "applied"
    assert payload["due"][0]["erasure_receipt_ref"] is not None
    assert d.exists()  # ciphertext object untouched
    store = open_dek_store(home)
    assert store.get_dek("user@example.com") is None  # DEK destroyed via ADR-0069 store
    store.close()
    receipt = json.loads(Path(payload["due"][0]["erasure_receipt_ref"]).read_text())
    assert receipt["legal_basis"] == "GDPR Art.17"


def test_status_human_output_and_disabled_binding(workspace: Path) -> None:
    policy = workspace / ".novafabric" / "registries" / REGISTRY / "retention-policy.yaml"
    policy.write_text(
        policy.read_text()
        + "  - id: disabled-one\n"
        + "    match:\n      tag: trade\n"
        + "    window: P1D\n    action: purge\n    enabled: false\n"
    )
    result = runner.invoke(
        app, ["retention", "status", "--registry", REGISTRY], catch_exceptions=False
    )
    assert result.exit_code == 0
    assert "old-trades-purge" in result.output
    assert "disabled-one" not in result.output

    (workspace / ".novafabric" / "registries" / "empty3").mkdir(parents=True)
    empty = runner.invoke(
        app, ["retention", "status", "--registry", "empty3"], catch_exceptions=False
    )
    assert "No enabled retention bindings" in empty.output


def test_explain_human_output_with_holds_and_expired_marker(workspace: Path) -> None:
    worm_db = workspace / ".novafabric" / "registries" / REGISTRY / "worm.db"
    LocalWormAdapter(worm_db).put("cap-old", b"data", retention_days=3650)
    holds_path = workspace / ".novafabric" / "registries" / REGISTRY / "holds.jsonl"
    holds_path.write_text(
        json.dumps(
            {
                "hold_id": "lit-9",
                "registry": REGISTRY,
                "reason": "litigation",
                "duration_days": None,
                "created_at": NOW.isoformat(),
                "released_at": None,
            }
        )
        + "\n"
    )
    (_capsule_dir(workspace, "cap-old") / "retention-expired.json").write_text("{}")
    result = runner.invoke(
        app, ["retention", "explain", "cap-old", "--registry", REGISTRY],
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    assert "lit-9" in result.output
    assert "WORM locked until" in result.output
    assert "already expired" in result.output
    assert "old-trades-purge" in result.output

    other = runner.invoke(
        app, ["retention", "explain", "cap-other", "--registry", REGISTRY],
        catch_exceptions=False,
    )
    assert "never swept" in other.output
