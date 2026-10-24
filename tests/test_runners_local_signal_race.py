"""A SIGTERM that arrives just after the workload starts is forwarded, not fatal.

``LocalRunner`` installed its forwarding handler only after ``Popen`` returned. A
signal in that window — the workload already running, the handler not yet set —
killed ``nova capture`` outright (exit -15), the workload ran on unsupervised and
no capsule was finalised. Seen as a flake of
``test_sigterm_to_a_real_capture_leaves_a_finalised_aborted_capsule`` at load 21.
The handler is now installed before the spawn, and a signal that lands before the
process group is known is delivered as soon as it is.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from novafabric.runners import LocalRunner, RunnerJobSpec, _local
from novafabric.runners._types import interrupted_result


def test_sigterm_right_after_spawn_stops_the_workload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_popen = subprocess.Popen
    spawned: list[subprocess.Popen[bytes]] = []

    def popen_then_sigterm(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        os.kill(os.getpid(), signal.SIGTERM)  # lands before Popen has even returned
        return proc

    monkeypatch.setattr(_local.subprocess, "Popen", popen_then_sigterm)
    spec = RunnerJobSpec(
        run_id="01TESTRACE0000000000000000", command=["sleep", "30"],
        capsule_dir=tmp_path, env={"PATH": os.environ["PATH"]}, timeout_s=60.0,
    )
    with pytest.raises(SystemExit) as exc_info:
        LocalRunner().run(spec)
    assert exc_info.value.code == 128 + signal.SIGTERM
    (proc,) = spawned
    assert proc.poll() is not None, "the workload was left running"
    result = interrupted_result(exc_info.value)
    assert result is not None and result.exit_code == 128 + signal.SIGTERM
    assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL or callable(
        signal.getsignal(signal.SIGTERM)
    )
