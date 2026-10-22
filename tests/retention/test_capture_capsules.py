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
"""The retention sweep sees ``capsule.yaml`` capsules; ``apply`` acts on them only on opt-in.

``nova capture`` and the framework adapters write ``capsule.yaml``; the sweep used to
enumerate ``capsule.json`` only, so no retention action ever reached a capsule
NovaFabric writes. Because there is no daemon and operators run ``nova retention
apply --yes`` from cron, simply widening enumeration would start deleting on
upgrade. The decision implemented here:

* ``plan``, ``apply --dry-run``, ``status`` and ``explain`` always enumerate
  ``capsule.yaml`` capsules and show what would happen to them;
* ``apply`` acts on them only when ``retention-policy.yaml`` sets
  ``include_capture_capsules: true`` (optional, default off);
* without the opt-in, ``plan`` and ``apply`` say how many capture capsules are
  excluded and how to include them;
* holds, WORM locks and the apply-time re-check apply to them exactly as to
  ``capsule.json`` capsules.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.retention.holds import place_hold
from novafabric.retention.sweep import (
    MANIFEST_JSON,
    MANIFEST_YAML,
    enumerate_capsules,
    load_sweep_options,
)
from novafabric.storage._local_worm import LocalWormAdapter

runner = CliRunner()
REGISTRY = "prod"
NOW = datetime.now(tz=timezone.utc)
OLD = NOW - timedelta(days=400)

_POLICY = """
version: 1
registry: prod
retention_days: 30
deletion_mode: defensible
{extra}
bindings:
  - id: purge-old
    match:
      age: P30D
    window: P90D
    action: purge
"""


def _policy(cwd: Path, *, opt_in: bool | None) -> Path:
    path = cwd / ".novafabric" / "registries" / REGISTRY / "retention-policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    extra = "" if opt_in is None else f"include_capture_capsules: {str(opt_in).lower()}"
    path.write_text(_POLICY.format(extra=extra))
    return path


def _yaml_capsule(capsules: Path, run_id: str, created_at: datetime = OLD, **extra: object) -> Path:
    """A capsule shaped like ``nova capture`` writes it (capsule.yaml, top-level env)."""
    d = capsules / run_id
    d.mkdir(parents=True, exist_ok=True)
    manifest = {"run_id": run_id, "created_at": created_at.isoformat(), **extra}
    (d / "capsule.yaml").write_text(yaml.dump(manifest))
    return d


def _json_capsule(capsules: Path, run_id: str, created_at: datetime = OLD) -> Path:
    d = capsules / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "capsule.json").write_text(
        json.dumps({"run_id": run_id, "created_at": created_at.isoformat()})
    )
    return d


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """cwd (registry config) + NOVAFABRIC_HOME with one capture and one legacy capsule."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    capsules = tmp_path / "home" / "capsules"
    _yaml_capsule(capsules, "cap-yaml")
    _json_capsule(capsules, "cap-json")
    return tmp_path


def _cap(ws: Path, run_id: str) -> Path:
    return ws / "home" / "capsules" / run_id


def _audit_ids(ws: Path) -> list[str]:
    path = ws / "audit.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["resource_id"] for line in path.read_text().splitlines() if line]


def _invoke(*args: str) -> object:
    result = runner.invoke(app, ["retention", *args])
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        result.exception
    )
    return result


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def test_enumerate_sees_capsule_yaml_with_capture_fields(tmp_path: Path) -> None:
    capsules = tmp_path / "capsules"
    _yaml_capsule(
        capsules, "01RUN", deployment_environment="production", alias="agent-a",
        metadata={"tags": "contains-pii, trade"},
    )
    [item] = enumerate_capsules(capsules)
    assert item.item_id == "01RUN"
    assert item.manifest == MANIFEST_YAML
    assert item.created_at == OLD
    # nova capture writes deployment_environment at the top level (ADR-0126).
    assert item.deployment_environment == "production"
    assert item.asset_ids == ["agent-a"]
    assert item.tags == {"contains-pii", "trade"}


def test_enumerate_marks_legacy_capsules_json(tmp_path: Path) -> None:
    _json_capsule(tmp_path / "capsules", "cap-json")
    [item] = enumerate_capsules(tmp_path / "capsules")
    assert item.manifest == MANIFEST_JSON


def test_a_directory_with_both_manifests_is_one_capsule_json_item(tmp_path: Path) -> None:
    """Pre-change behaviour for such a directory is kept: it is swept as capsule.json."""
    d = _json_capsule(tmp_path / "capsules", "both")
    (d / "capsule.yaml").write_text(yaml.dump({"run_id": "both", "created_at": OLD.isoformat()}))
    items = enumerate_capsules(tmp_path / "capsules")
    assert [(i.item_id, i.manifest) for i in items] == [("both", MANIFEST_JSON)]


def test_an_unreadable_capsule_yaml_is_skipped_never_swept(tmp_path: Path) -> None:
    d = tmp_path / "capsules" / "broken"
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_text("run_id: [unterminated\n")
    assert enumerate_capsules(tmp_path / "capsules") == []


def test_yaml_timestamp_without_zone_is_read_as_utc(tmp_path: Path) -> None:
    d = tmp_path / "capsules" / "ts"
    d.mkdir(parents=True)
    (d / "capsule.yaml").write_text("run_id: ts\ncreated_at: 2025-01-02 03:04:05\n")
    [item] = enumerate_capsules(tmp_path / "capsules")
    assert item.created_at == datetime(2025, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The opt-in key
# ---------------------------------------------------------------------------


def test_opt_in_defaults_off(tmp_path: Path) -> None:
    assert load_sweep_options(tmp_path / "absent.yaml").include_capture_capsules is False
    _policy(tmp_path, opt_in=None)
    path = tmp_path / ".novafabric" / "registries" / REGISTRY / "retention-policy.yaml"
    assert load_sweep_options(path).include_capture_capsules is False


def test_opt_in_is_read(tmp_path: Path) -> None:
    path = _policy(tmp_path, opt_in=True)
    assert load_sweep_options(path).include_capture_capsules is True


def test_a_non_boolean_opt_in_is_rejected(tmp_path: Path) -> None:
    path = _policy(tmp_path, opt_in=None)
    path.write_text(path.read_text() + "include_capture_capsules: sometimes\n")
    with pytest.raises(ValueError, match="include_capture_capsules"):
        load_sweep_options(path)


def test_cli_refuses_a_non_boolean_opt_in(ws: Path) -> None:
    path = _policy(ws / "cwd", opt_in=None)
    path.write_text(path.read_text() + "include_capture_capsules: yes please\n")
    result = _invoke("apply", "--registry", REGISTRY, "--yes")
    assert result.exit_code == 1
    assert _cap(ws, "cap-json").is_dir() and _cap(ws, "cap-yaml").is_dir()


# ---------------------------------------------------------------------------
# Without the opt-in: shown, never acted on, and the operator is told
# ---------------------------------------------------------------------------


def test_plan_shows_what_would_happen_to_capture_capsules(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=None)
    result = _invoke("plan", "--registry", REGISTRY, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [d["item_id"] for d in payload["due"]] == ["cap-json"]
    cc = payload["capture_capsules"]
    assert cc["included"] is False
    assert cc["enumerated"] == 1
    assert [(r["item_id"], r["action"], r["outcome"]) for r in cc["excluded"]] == [
        ("cap-yaml", "purge", "dry-run")
    ]
    assert "include_capture_capsules: true" in cc["notice"]
    assert _cap(ws, "cap-yaml").is_dir() and _cap(ws, "cap-json").is_dir()


def test_plan_table_prints_the_exclusion_notice(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=None)
    result = _invoke("plan", "--registry", REGISTRY)
    assert result.exit_code == 0, result.output
    text = " ".join(result.stdout.split())
    assert "cap-yaml" in text
    assert "1 capture capsule(s)" in text
    assert "include_capture_capsules: true" in text


def test_apply_without_opt_in_never_touches_a_capture_capsule(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=None)
    result = _invoke("apply", "--registry", REGISTRY, "--yes")
    assert result.exit_code == 0, result.output
    assert not _cap(ws, "cap-json").exists(), "the legacy capsule should be purged"
    assert _cap(ws, "cap-yaml").is_dir(), "a capture capsule was purged without opt-in"
    assert "include_capture_capsules: true" in " ".join(result.stdout.split())
    # Out-of-scope items are not swept, so they leave no evidence entry.
    assert _audit_ids(ws) == ["cap-json"]


def test_apply_json_without_opt_in_reports_the_exclusion(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=False)
    result = _invoke("apply", "--registry", REGISTRY, "--yes", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["capture_capsules"]["included"] is False
    assert [r["item_id"] for r in payload["capture_capsules"]["excluded"]] == ["cap-yaml"]
    assert _cap(ws, "cap-yaml").is_dir()


def test_apply_dry_run_matches_plan(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=None)
    plan = json.loads(_invoke("plan", "--registry", REGISTRY, "--json").stdout)
    dry = json.loads(_invoke("apply", "--registry", REGISTRY, "--dry-run", "--json").stdout)
    for payload in (plan, dry):
        for r in payload["due"] + payload["capture_capsules"]["excluded"]:
            for volatile in ("record_id", "swept_at", "principal"):
                r.pop(volatile)
    assert dry["due"] == plan["due"]
    assert dry["capture_capsules"] == plan["capture_capsules"]


def test_confirmation_counts_only_what_apply_will_act_on(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(ws / "cwd", opt_in=None)
    prompts: list[str] = []

    def confirm(text: str, *_: object, **__: object) -> bool:
        prompts.append(text)
        return False

    monkeypatch.setattr("typer.confirm", confirm)
    result = _invoke("apply", "--registry", REGISTRY)
    assert result.exit_code == 1
    assert prompts == ["Apply 1 retention action(s)?"]


def test_status_counts_capture_capsules_separately(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=None)
    payload = json.loads(_invoke("status", "--registry", REGISTRY, "--json").stdout)
    assert payload["include_capture_capsules"] is False
    assert payload["capture_capsules_enumerated"] == 1
    [row] = payload["bindings"]
    assert (row["due_now"], row["held"], row["excluded_capture_capsules"]) == (1, 0, 1)


def test_status_table_prints_the_exclusion_notice(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=None)
    result = _invoke("status", "--registry", REGISTRY)
    assert result.exit_code == 0, result.output
    assert "include_capture_capsules: true" in " ".join(result.stdout.split())


def test_explain_finds_a_capture_capsule_and_says_apply_skips_it(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=None)
    result = _invoke("explain", "cap-yaml", "--registry", REGISTRY, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["manifest"] == "capsule.yaml"
    assert payload["swept_by_apply"] is False
    assert [m["binding_id"] for m in payload["matched_bindings"]] == ["purge-old"]


# ---------------------------------------------------------------------------
# With the opt-in: acted on, under exactly the same holds
# ---------------------------------------------------------------------------


def test_apply_with_opt_in_purges_a_capture_capsule(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=True)
    result = _invoke("apply", "--registry", REGISTRY, "--yes", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert sorted(r["item_id"] for r in payload["due"]) == ["cap-json", "cap-yaml"]
    assert payload["capture_capsules"] == {
        "included": True, "enumerated": 1, "excluded": [], "notice": None,
    }
    assert not _cap(ws, "cap-yaml").exists()
    assert sorted(_audit_ids(ws)) == ["cap-json", "cap-yaml"]


def test_status_with_opt_in_counts_capture_capsules_as_due(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=True)
    payload = json.loads(_invoke("status", "--registry", REGISTRY, "--json").stdout)
    [row] = payload["bindings"]
    assert (row["due_now"], row["excluded_capture_capsules"]) == (2, 0)


def test_a_worm_lock_holds_a_capture_capsule(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=True)
    worm = ws / "worm.db"
    LocalWormAdapter(worm).put("cap-yaml", b"x", retention_days=3650)
    result = _invoke("apply", "--registry", REGISTRY, "--yes", "--json", "--worm-db", str(worm))
    assert result.exit_code == 0, result.output
    held = json.loads(result.stdout)["held"]
    assert [(r["item_id"], r["reason"]) for r in held] == [("cap-yaml", "worm_hold")]
    assert _cap(ws, "cap-yaml").is_dir()


def test_a_legal_hold_holds_a_capture_capsule(ws: Path) -> None:
    _policy(ws / "cwd", opt_in=True)
    place_hold(REGISTRY, "litigation", actor="legal")
    result = _invoke("apply", "--registry", REGISTRY, "--yes", "--json")
    assert result.exit_code == 0, result.output
    assert _cap(ws, "cap-yaml").is_dir() and _cap(ws, "cap-json").is_dir()


def test_a_hold_placed_at_the_prompt_still_wins_for_a_capture_capsule(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _policy(ws / "cwd", opt_in=True)

    def confirm_while_a_hold_lands(*_: object, **__: object) -> bool:
        place_hold(REGISTRY, "subpoena arrived", actor="legal")
        return True

    monkeypatch.setattr("typer.confirm", confirm_while_a_hold_lands)
    result = _invoke("apply", "--registry", REGISTRY, "--json")
    assert result.exit_code == 0, result.output
    assert _cap(ws, "cap-yaml").is_dir()
    assert _cap(ws, "cap-json").is_dir()
    # The prompt text precedes the JSON on stdout; both items were re-checked.
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    assert {r["item_id"]: r["outcome"] for r in payload["held"]} == {
        "cap-json": "skipped", "cap-yaml": "skipped",
    }


@pytest.mark.parametrize("opt_in", [True, False])
def test_plan_shows_a_worm_locked_capture_capsule_as_held(ws: Path, opt_in: bool) -> None:
    """The planner resolves WORM locks for capsule.yaml exactly as for capsule.json."""
    _policy(ws / "cwd", opt_in=opt_in)
    worm = ws / "worm.db"
    LocalWormAdapter(worm).put("cap-yaml", b"x", retention_days=3650)
    result = _invoke("plan", "--registry", REGISTRY, "--json", "--worm-db", str(worm))
    payload = json.loads(result.stdout)
    rows = payload["held"] if opt_in else payload["capture_capsules"]["excluded"]
    assert [(r["item_id"], r["outcome"], r["reason"]) for r in rows] == [
        ("cap-yaml", "skipped", "worm_hold")
    ]
