"""``nova capsule delete`` reports and audits only a deletion that happened.

Attack (adversarial review, 2026-10-22): with an indefinite legal hold on the
registry, ``nova capsule delete <id> --registry default --force`` printed
"Deleted capsule", exited 0 and appended a ``capsule.delete`` audit event -- and
removed nothing from disk. ``--force`` (documented as "skip the retention check")
also skipped the hold check, and the retention policy was read from a path
relative to the shell's working directory.

Pinned here:

* holds always win, ``--force`` included, and in every registry (the governed
  delete core's rule: holds are registry-global);
* success is printed and ``capsule.delete`` audited only after the capsule
  directory is actually gone (the governed ``execute_delete`` core);
* a missing capsule is an error, not a success;
* the retention policy is read from ``$NOVAFABRIC_HOME/registries/<r>/`` and the
  legacy ``./.novafabric/registries/<r>/``; ``--force`` skips only its retention
  window, never ``deletion_mode: prohibited``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.retention.holds import place_hold

runner = CliRunner()
RUN = "01M5JDD2Y0VKD0XK5YZ4CZNND8"


class WS:
    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.cwd = tmp_path / "cwd"
        self.capsules = self.home / "capsules"
        self.audit = tmp_path / "audit.jsonl"

    def capsule(self, run_id: str = RUN) -> Path:
        d = self.capsules / run_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "capsule.yaml").write_text(f"run_id: {run_id}\n")
        return d

    def audit_events(self) -> list[str]:
        if not self.audit.exists():
            return []
        return [
            json.loads(line)["event_type"]
            for line in self.audit.read_text().splitlines()
            if line.strip()
        ]

    def policy(self, root: Path, registry: str, body: str) -> None:
        d = root / "registries" / registry
        d.mkdir(parents=True, exist_ok=True)
        (d / "retention-policy.yaml").write_text(body)


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WS:
    w = WS(tmp_path)
    w.cwd.mkdir()
    monkeypatch.chdir(w.cwd)
    monkeypatch.setenv("NOVAFABRIC_HOME", str(w.home))
    monkeypatch.delenv("NOVAFABRIC_CAPSULE_DIR", raising=False)
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(w.audit))
    return w


def _delete(*extra: str, registry: str = "default") -> object:
    res = runner.invoke(app, ["capsule", "delete", RUN, "--registry", registry, *extra])
    assert isinstance(res.exception, SystemExit) or res.exception is None, res.exception
    return res


def test_force_under_a_hold_is_refused_and_not_reported_as_deleted(ws: WS) -> None:
    cap = ws.capsule()
    place_hold("default", "litigation", actor="legal")
    res = _delete("--force")
    assert res.exit_code == 1, res.output
    assert "Deleted" not in res.output
    assert "legal hold" in res.output
    assert cap.is_dir()
    assert "capsule.delete" not in ws.audit_events()


def test_a_hold_on_another_registry_still_blocks(ws: WS) -> None:
    """Naming an unheld registry is not a way around a hold (holds are registry-global)."""
    cap = ws.capsule()
    place_hold("litigation-a", "litigation", actor="legal")
    res = _delete(registry="unheld")
    assert res.exit_code == 1, res.output
    assert cap.is_dir()


def test_force_with_an_unreadable_worm_db_is_refused(ws: WS) -> None:
    """Unknown WORM state is not "unlocked" (ADR-0318), ``--force`` included."""
    cap = ws.capsule()
    reg = ws.home / "registries" / "default"
    reg.mkdir(parents=True)
    (reg / "worm.db").write_bytes(b"not a sqlite database")
    res = _delete("--force")
    assert res.exit_code == 1, res.output
    assert "worm_state_unavailable" in res.output
    assert "Deleted" not in res.output
    assert cap.is_dir()
    assert "capsule.delete" not in ws.audit_events()


def test_a_successful_delete_removes_the_capsule(ws: WS) -> None:
    cap = ws.capsule()
    res = _delete()
    assert res.exit_code == 0, res.output
    assert "Deleted capsule" in res.output
    assert not cap.exists()
    assert ws.audit_events().count("capsule.delete") == 1


def test_a_missing_capsule_is_an_error_not_a_success(ws: WS) -> None:
    res = _delete()
    assert res.exit_code == 1, res.output
    assert "Deleted" not in res.output
    assert "not found" in res.output
    assert "capsule.delete" not in ws.audit_events()


_WINDOW = "registry: default\nretention_days: 3650\ndeletion_mode: defensible\nlegal_hold_ids: []\n"
_PROHIBITED = "registry: default\nretention_days: 1\ndeletion_mode: prohibited\nlegal_hold_ids: []\n"


def test_policy_under_nova_home_is_enforced(ws: WS) -> None:
    cap = ws.capsule()
    ws.policy(ws.home, "default", _WINDOW)
    res = _delete()
    assert res.exit_code == 1, res.output
    assert "retention window" in res.output
    assert cap.is_dir()
    # --force skips the retention window, and only that.
    res = _delete("--force")
    assert res.exit_code == 0, res.output
    assert not cap.exists()


def test_legacy_cwd_policy_is_still_read(ws: WS) -> None:
    cap = ws.capsule()
    ws.policy(ws.cwd / ".novafabric", "default", _WINDOW)
    res = _delete()
    assert res.exit_code == 1, res.output
    assert cap.is_dir()


def test_force_does_not_override_deletion_prohibited(ws: WS) -> None:
    cap = ws.capsule()
    ws.policy(ws.home, "default", _PROHIBITED)
    res = _delete("--force", "--age-days", "100")
    assert res.exit_code == 1, res.output
    assert "prohibited" in res.output
    assert cap.is_dir()
