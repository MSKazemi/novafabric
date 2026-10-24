"""``nova capture`` keeps the finalisation result: a seal that fails is recorded and shown.

Review capture-runners-21: the orchestrator took ``finalize_capsule(...).manifest`` and
dropped ``sealed`` / ``unsealed_reason``. With a signing profile configured, a TSA
outage, a locked Merkle DB or a manifest-gate refusal left one stderr line, nothing
in the capsule, and ``✓ Capsule written`` — while adapter and ``@agent`` capsules
record the same condition in ``metadata.finalization_error``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.capture import orchestrator
from novafabric.capture.orchestrator import CaptureOrchestrator

REASON = "NovaSeal seal failed: TSA unreachable"


@pytest.fixture
def failing_seal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orchestrator, "_seal_capsule", lambda *_a, **_k: REASON)


def _manifest(capsule_dir: Path) -> dict:  # type: ignore[type-arg]
    loaded = yaml.safe_load((capsule_dir / "capsule.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.usefixtures("failing_seal")
def test_the_reason_is_in_the_result_and_the_capsule(tmp_path: Path) -> None:
    result = CaptureOrchestrator(base_dir=tmp_path).run(["true"])
    assert result.sealed is False
    assert result.unsealed_reason == REASON
    assert _manifest(result.capsule_dir)["metadata"]["finalization_error"] == REASON
    assert result.exit_code == 0  # the workload's own outcome is unchanged


def test_a_capsule_without_a_signing_profile_carries_no_reason(tmp_path: Path) -> None:
    result = CaptureOrchestrator(base_dir=tmp_path).run(["true"])
    assert result.unsealed_reason is None
    assert "finalization_error" not in (_manifest(result.capsule_dir).get("metadata") or {})


@pytest.mark.usefixtures("failing_seal")
def test_the_cli_says_the_capsule_is_not_sealed(tmp_path: Path) -> None:
    from novafabric.cli.main import app

    res = CliRunner().invoke(app, ["capture", "--no-daemon", "--output-dir", str(tmp_path), "--", "true"])
    assert res.exit_code == 0, res.output
    assert "not sealed" in res.output and "TSA unreachable" in res.output
