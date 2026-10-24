"""The capture loader runs the user's own ``sitecustomize`` after installing the hooks.

Review capture-runners-09: Python imports exactly one ``sitecustomize`` — the first
on ``sys.path``. Under capture that was always NovaFabric's loader, so the user's
own (on their ``PYTHONPATH``, in the venv, or the distro's) was silently skipped,
and the workload under capture was not the program the user ran: certificates,
proxies, warning filters and coverage set up there were missing.

These run a real interpreter with the loader directory ahead of the user's.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from novafabric.runners._sitecustomize import HOOK_LOADER


def _run(tmp_path: Path, user_site: str | None, code: str) -> subprocess.CompletedProcess[str]:
    loader = tmp_path / "loader"
    loader.mkdir()
    (loader / "sitecustomize.py").write_text(HOOK_LOADER)
    path = [str(loader)]
    if user_site is not None:
        user = tmp_path / "user"
        user.mkdir()
        (user / "sitecustomize.py").write_text(user_site)
        path.append(str(user))
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOVAFABRIC_CAPSULE")}
    env["PYTHONPATH"] = os.pathsep.join(path)
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )


def test_the_users_sitecustomize_still_runs(tmp_path: Path) -> None:
    done = _run(
        tmp_path,
        "import os\nos.environ['USER_SITECUSTOMIZE_RAN'] = 'yes'\n",
        "import os, sitecustomize; print(os.environ.get('USER_SITECUSTOMIZE_RAN'), "
        "sitecustomize.__file__)",
    )
    assert done.returncode == 0, done.stderr
    ran, module_file = done.stdout.split()
    assert ran == "yes"
    assert module_file.endswith(os.path.join("user", "sitecustomize.py"))


def test_a_failing_user_sitecustomize_does_not_stop_the_workload(tmp_path: Path) -> None:
    done = _run(tmp_path, "raise RuntimeError('broken user site')\n", "print('workload ran')")
    assert done.returncode == 0
    assert done.stdout.strip() == "workload ran"
    assert "Error in sitecustomize" in done.stderr and "broken user site" in done.stderr


def test_without_a_user_sitecustomize_nothing_changes(tmp_path: Path) -> None:
    done = _run(tmp_path, None, "print('ok')")
    assert done.returncode == 0 and done.stdout.strip() == "ok"
    assert done.stderr == ""


def test_two_loaders_on_the_path_do_not_chain_into_each_other(tmp_path: Path) -> None:
    """A capture nested in a capture puts two loader directories on the path."""
    second = tmp_path / "loader2"
    second.mkdir()
    (second / "sitecustomize.py").write_text(HOOK_LOADER)
    loader = tmp_path / "loader"
    loader.mkdir()
    (loader / "sitecustomize.py").write_text(HOOK_LOADER)
    user = tmp_path / "user"
    user.mkdir()
    (user / "sitecustomize.py").write_text("import os\nos.environ['USER_RAN'] = 'yes'\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("NOVAFABRIC_CAPSULE")}
    env["PYTHONPATH"] = os.pathsep.join([str(loader), str(second), str(user)])
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('USER_RAN'))"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "yes"
    assert "RecursionError" not in done.stderr
