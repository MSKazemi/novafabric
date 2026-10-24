"""An interrupted ``nova capture`` still finalises its capsule (review capture-runners-03).

Ctrl-C or SIGTERM used to leave the capsule with no ``capsule.yaml``, no
redaction proof and — the part that matters — **no secret scan**: the raw
``model-calls.jsonl`` the workload had written stayed on disk unredacted. Once
the runner has stopped the workload, the orchestrator now finalises the capsule
on the normal path with ``status: aborted`` and then re-raises the original
interruption, so ``nova capture``'s exit code (130 / 128+signal) is unchanged.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.runners._types import (
    RunnerJobResult,
    RunnerJobSpec,
    attach_interrupted_result,
    interrupted_result,
)

SECRET = "sk-ant-api03-" + "Q" * 93  # an Anthropic-key shape the built-in scanner redacts


class _InterruptedRunner:
    """Writes a model call carrying a secret, then is 'interrupted' like LocalRunner is."""

    name = "local"
    description = "test double"

    def __init__(self, exc: BaseException, exit_code: int) -> None:
        self._exc = exc
        self._exit_code = exit_code

    def supports(self) -> tuple[bool, str]:
        return True, ""

    def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
        record = {"model_call_id": "m1", "request": {"messages": [{"content": SECRET}]}}
        with (spec.capsule_dir / "model-calls.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        attach_interrupted_result(self._exc, RunnerJobResult(
            exit_code=self._exit_code,
            runner_status="interrupted",
            stdout=b"partial output\n",
            runner_error="interrupted by SIGINT",
        ))
        raise self._exc


def _only_capsule(base: Path) -> Path:
    found = [d for d in base.iterdir() if d.is_dir() and not d.name.startswith(".")]
    assert len(found) == 1, found
    return found[0]


def test_the_result_rides_on_the_interruption_itself() -> None:
    exc = KeyboardInterrupt()
    assert interrupted_result(exc) is None
    result = RunnerJobResult(exit_code=130, runner_status="interrupted")
    attach_interrupted_result(exc, result)
    assert interrupted_result(exc) is result
    assert interrupted_result(ValueError()) is None


@pytest.mark.parametrize(
    ("exc", "exit_code"),
    [(KeyboardInterrupt(), 130), (SystemExit(143), 143)],
    ids=["ctrl-c", "sigterm"],
)
def test_an_interrupted_capture_is_finalised_then_re_raised(
    tmp_path: Path, exc: BaseException, exit_code: int
) -> None:
    base = tmp_path / "caps"
    orch = CaptureOrchestrator(base_dir=base, runner=_InterruptedRunner(exc, exit_code))
    with pytest.raises(type(exc)) as raised:
        orch.run(["agent.py"])
    assert raised.value is exc  # the very same interruption, not a substitute

    cap = _only_capsule(base)
    manifest = yaml.safe_load((cap / "capsule.yaml").read_text())
    assert manifest["status"] == "aborted"
    assert manifest["exit_code"] == exit_code
    assert manifest["error"]["type"] == "WorkloadInterrupted"
    assert (cap / "redaction-proof.json").is_file()
    assert SECRET not in (cap / "model-calls.jsonl").read_text()  # the scan ran
    assert (cap / "outputs" / "stdout.txt").read_bytes() == b"partial output\n"


def test_an_interruption_without_a_result_is_not_finalised(tmp_path: Path) -> None:
    """A runner that did not stop its workload attaches nothing; the capsule is not
    finalised over a workload that may still be writing into it."""

    class _Bare(_InterruptedRunner):
        def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
            raise KeyboardInterrupt

    base = tmp_path / "caps"
    with pytest.raises(KeyboardInterrupt):
        CaptureOrchestrator(base_dir=base, runner=_Bare(KeyboardInterrupt(), 130)).run(["x"])
    assert not (_only_capsule(base) / "capsule.yaml").exists()


# ------------------------------------------------------------- end to end

_DRIVER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from novafabric.runners import _local
    from novafabric.capture.orchestrator import CaptureOrchestrator
    _local.INTERRUPT_GRACE_S = 2.0
    CaptureOrchestrator(base_dir=Path(sys.argv[1])).run(["sh", "-c", sys.argv[2]])
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_to_a_real_capture_leaves_a_finalised_aborted_capsule(tmp_path: Path) -> None:
    base, pidfile = tmp_path / "caps", tmp_path / "workload.pid"
    env: dict[str, Any] = {k: v for k, v in os.environ.items() if k != "NOVAFABRIC_CAPSULE_DIR"}
    driver = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(base), f"echo $$ > {pidfile}; exec sleep 60"],
        env=env,
    )
    deadline = time.monotonic() + 60
    while not (pidfile.exists() and pidfile.read_text().strip()):
        assert time.monotonic() < deadline, "workload never started"
        assert driver.poll() is None, "driver exited early"
        time.sleep(0.05)
    driver.send_signal(signal.SIGTERM)
    assert driver.wait(timeout=60) == 128 + signal.SIGTERM

    manifest = yaml.safe_load((_only_capsule(base) / "capsule.yaml").read_text())
    assert manifest["status"] == "aborted"
    assert manifest["exit_code"] == 128 + signal.SIGTERM
