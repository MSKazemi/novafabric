"""``nova retention`` reads its per-registry files from ``$NOVAFABRIC_HOME/registries``.

``cli/retention.py`` resolved ``retention-policy.yaml`` (which carries the
``bindings:`` block and the sweep switches) and ``worm.db`` as
``./.novafabric/registries/<registry>/`` under the **current working directory**.
Run from cron, a container entrypoint or any other directory, the sweep found no
bindings (nothing swept) and no WORM database (no WORM lock honoured by the
planner), while ``nova hold`` and ``POST /api/holds`` already shared
``$NOVAFABRIC_HOME/registries/<registry>/``.

The files now resolve like the hold store (:mod:`novafabric.retention.holds`):
canonical ``$NOVAFABRIC_HOME/registries/<r>/`` first, the legacy working-directory
copy still read — with a warning — when it exists. Deletion gates fail closed across
copies: ``deletion_mode: prohibited`` in any copy and a WORM lock in any ``worm.db``
both hold the item.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.storage._local_worm import LocalWormAdapter

runner = CliRunner()
REGISTRY = "prod"
OLD = datetime.now(tz=timezone.utc) - timedelta(days=400)

_POLICY = """
version: 1
registry: prod
retention_days: 30
deletion_mode: {mode}
bindings:
  - id: {binding}
    match:
      age: P30D
    window: P90D
    action: purge
"""


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home with one due ``capsule.json`` capsule, and an unrelated cwd."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    cap = tmp_path / "home" / "capsules" / "cap-json"
    cap.mkdir(parents=True)
    (cap / "capsule.json").write_text(
        json.dumps({"run_id": "cap-json", "created_at": OLD.isoformat()})
    )
    return tmp_path


def _canonical(ws: Path) -> Path:
    return ws / "home" / "registries" / REGISTRY


def _legacy(ws: Path) -> Path:
    return ws / "cwd" / ".novafabric" / "registries" / REGISTRY


def _policy(registry_dir: Path, *, binding: str = "purge-old", mode: str = "defensible") -> Path:
    registry_dir.mkdir(parents=True, exist_ok=True)
    path = registry_dir / "retention-policy.yaml"
    path.write_text(_POLICY.format(binding=binding, mode=mode))
    return path


def _lock(registry_dir: Path, capsule_id: str = "cap-json") -> Path:
    registry_dir.mkdir(parents=True, exist_ok=True)
    db = registry_dir / "worm.db"
    LocalWormAdapter(db).put(capsule_id, b"x", retention_days=3650)
    return db


def _plan(*extra: str) -> dict[str, Any]:
    result = runner.invoke(app, ["retention", "plan", "--registry", REGISTRY, "--json", *extra])
    assert result.exit_code == 0, (result.exception, result.output)
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def _rows(payload: dict[str, Any], key: str) -> list[tuple[str, str, str | None]]:
    return [(r["binding_id"], r["item_id"], r.get("reason")) for r in payload[key]]


def _legacy_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "legacy" in r.getMessage()
    ]


# ---------------------------------------------------------------------------
# retention-policy.yaml (bindings + sweep switches + deletion_mode)
# ---------------------------------------------------------------------------


def test_bindings_are_read_from_the_home_registry(ws: Path) -> None:
    _policy(_canonical(ws))
    assert _rows(_plan(), "due") == [("purge-old", "cap-json", None)]


def test_apply_acts_on_home_registry_bindings_from_any_cwd(ws: Path) -> None:
    _policy(_canonical(ws))
    result = runner.invoke(app, ["retention", "apply", "--registry", REGISTRY, "--yes", "--json"])
    assert result.exit_code == 0, (result.exception, result.output)
    assert [r["item_id"] for r in json.loads(result.stdout)["due"]] == ["cap-json"]
    assert not (ws / "home" / "capsules" / "cap-json").exists()


def test_legacy_cwd_policy_is_still_read_with_a_warning(
    ws: Path, caplog: pytest.LogCaptureFixture
) -> None:
    legacy = _policy(_legacy(ws))
    with caplog.at_level(logging.WARNING):
        assert _rows(_plan(), "due") == [("purge-old", "cap-json", None)]
    warnings = _legacy_warnings(caplog)
    assert any(str(legacy) in w and str(_canonical(ws)) in w for w in warnings), warnings


def test_home_policy_is_preferred_when_both_exist(
    ws: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _policy(_canonical(ws), binding="from-home")
    legacy = _policy(_legacy(ws), binding="from-cwd")
    with caplog.at_level(logging.WARNING):
        assert _rows(_plan(), "due") == [("from-home", "cap-json", None)]
    warnings = _legacy_warnings(caplog)
    assert any(str(legacy) in w for w in warnings), warnings


def test_no_warning_when_only_the_home_policy_exists(
    ws: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _policy(_canonical(ws))
    with caplog.at_level(logging.WARNING):
        _plan()
    assert _legacy_warnings(caplog) == []


def test_prohibited_in_a_shadowed_legacy_copy_still_blocks_purge(ws: Path) -> None:
    """Fail closed: a stale stricter copy is never silently ignored by a deletion gate."""
    _policy(_canonical(ws))
    _policy(_legacy(ws), mode="prohibited")
    payload = _plan()
    assert _rows(payload, "due") == []
    assert _rows(payload, "held") == [("purge-old", "cap-json", "deletion_prohibited")]


def test_explain_names_the_home_registry_file_when_none_exists(ws: Path) -> None:
    cap = ws / "home" / "capsules" / "cap-yaml"
    cap.mkdir()
    (cap / "capsule.yaml").write_text(f"run_id: cap-yaml\ncreated_at: '{OLD.isoformat()}'\n")
    result = runner.invoke(
        app, ["retention", "explain", "cap-yaml", "--registry", REGISTRY],
        env={"COLUMNS": "400"},
    )
    assert result.exit_code == 0, (result.exception, result.output)
    flat = "".join(result.output.split())
    assert "".join(str(_canonical(ws) / "retention-policy.yaml").split()) in flat, result.output


# ---------------------------------------------------------------------------
# worm.db
# ---------------------------------------------------------------------------


def test_worm_lock_in_the_home_registry_holds_the_item(ws: Path) -> None:
    _policy(_canonical(ws))
    _lock(_canonical(ws))
    payload = _plan()
    assert _rows(payload, "due") == []
    assert _rows(payload, "held") == [("purge-old", "cap-json", "worm_hold")]


def test_apply_never_purges_a_capsule_locked_in_the_home_worm_db(ws: Path) -> None:
    _policy(_canonical(ws))
    _lock(_canonical(ws))
    result = runner.invoke(app, ["retention", "apply", "--registry", REGISTRY, "--yes", "--json"])
    assert result.exit_code == 0, (result.exception, result.output)
    assert (ws / "home" / "capsules" / "cap-json").is_dir()


def test_legacy_cwd_worm_db_is_still_read_with_a_warning(
    ws: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _policy(_canonical(ws))
    legacy_db = _lock(_legacy(ws))
    with caplog.at_level(logging.WARNING):
        assert _rows(_plan(), "held") == [("purge-old", "cap-json", "worm_hold")]
    assert any(str(legacy_db) in w for w in _legacy_warnings(caplog))


def test_a_lock_in_either_worm_db_holds_when_both_exist(ws: Path) -> None:
    _policy(_canonical(ws))
    _lock(_canonical(ws), capsule_id="some-other-capsule")
    _lock(_legacy(ws))
    assert _rows(_plan(), "held") == [("purge-old", "cap-json", "worm_hold")]


def test_explicit_worm_db_flag_is_used(ws: Path) -> None:
    _policy(_canonical(ws))
    explicit = _lock(ws / "elsewhere")
    assert _rows(_plan("--worm-db", str(explicit)), "held") == [
        ("purge-old", "cap-json", "worm_hold")
    ]


# ---------------------------------------------------------------------------
# --help
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["plan", "apply", "status", "explain"])
def test_worm_db_help_states_the_home_default(command: str) -> None:
    result = runner.invoke(app, ["retention", command, "--help"], env={"COLUMNS": "400"})
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "$NOVAFABRIC_HOME/registries/<registry>/worm.db" in text, text
