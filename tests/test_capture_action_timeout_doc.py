"""L11: the GitHub Action's ``timeout`` input describes the real default.

``.github/actions/capture/action.yml`` said an empty ``timeout`` "means no
NovaFabric-imposed limit". The action then runs a plain ``nova capture`` (local
runner) without ``--timeout``, and the local runner applies a 600 s wall-clock
deadline. This pins the runner's real default by observing it, and the action's
description against it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.runners import LocalRunner, RunnerJobSpec
from novafabric.runners._local import DEFAULT_TIMEOUT_S

ACTION = Path(__file__).resolve().parents[1] / ".github" / "actions" / "capture" / "action.yml"


def test_local_runner_default_deadline_is_the_named_constant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}
    real_wait = subprocess.Popen.wait

    # The runner waits for the workload's exit (review capture-runners-10).
    def spy(self: subprocess.Popen[bytes], *args: Any, **kwargs: Any) -> Any:
        seen.setdefault("timeout", kwargs.get("timeout"))
        return real_wait(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "wait", spy)
    spec = RunnerJobSpec(
        run_id="01TESTRUN0000000000000000",
        command=["true"],
        capsule_dir=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )
    LocalRunner().run(spec)
    assert seen["timeout"] == DEFAULT_TIMEOUT_S == 600.0


def test_action_timeout_description_states_the_default() -> None:
    inputs = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["inputs"]
    description = inputs["timeout"]["description"]
    assert "no NovaFabric-imposed limit" not in description
    assert f"{int(DEFAULT_TIMEOUT_S)}" in description
    assert inputs["timeout"]["default"] == ""


def test_integration_doc_states_the_default() -> None:
    doc = (ACTION.parents[3] / "docs" / "integrations" / "github-actions.md").read_text()
    row = next(line for line in doc.splitlines() if line.startswith("| `timeout`"))
    assert "no NovaFabric-imposed limit" not in row
    assert f"{int(DEFAULT_TIMEOUT_S)} s" in row
