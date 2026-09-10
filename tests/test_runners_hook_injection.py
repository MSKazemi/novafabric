"""Every runner that starts a *separate* interpreter injects the capture hooks.

Wire-level capture only fires if the workload's interpreter loads NovaFabric's
hook loader at startup, and NovaFabric ships **no** auto-loading `sitecustomize`
or `.pth` — verified: `site-packages` contains no `sitecustomize.py`, and
`pyproject.toml` declares none. So each runner must do both halves itself:

1. materialise `sitecustomize.py` (the shared `HOOK_LOADER`) somewhere the
   workload's interpreter can see, and
2. put that directory on `PYTHONPATH`.

**Defect B3.** A runner that does neither still produces a capsule that reports
`status: success`, `exit_code: 0` and a complete, internally-consistent digest
manifest — over a file set from which every model call is simply absent. A
controlled comparison on real clusters measured 5 model calls under `local` and
`slurm`, and **0** under `kubernetes`, all three reporting success.

The bug was found on SLURM, fixed there in v0.6.11, and propagated to `_pbs` and
`_lsf` — but not to `_docker` or `_kubernetes`. Same shape as ADR-0270: a
property re-implemented per runner holds at N-1 sites. This test asserts it over
all of them at once, which is the assertion that was missing.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from novafabric.runners import DockerRunner, RunnerJobSpec
from novafabric.runners._sitecustomize import HOOK_LOADER


def _spec(tmp_path: Path, **kwargs: object) -> RunnerJobSpec:
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir(parents=True, exist_ok=True)
    return RunnerJobSpec(
        run_id="01TESTHOOKINJECT000000000",
        command=["python", "agent.py"],
        capsule_dir=capsule_dir,
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(capsule_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
        },
        **kwargs,  # type: ignore[arg-type]
    )


class TestTheLoaderIsNotAutoInstalled:
    """The premise the whole mechanism rests on."""

    def test_novafabric_ships_no_auto_loading_sitecustomize(self) -> None:
        import site

        for directory in site.getsitepackages():
            candidate = Path(directory) / "sitecustomize.py"
            assert not candidate.exists(), (
                f"{candidate} exists — if NovaFabric ever ships an auto-loading "
                "sitecustomize, the per-runner injection below becomes redundant "
                "and this whole test file needs rethinking, not deleting."
            )


class TestDockerInjectsTheHookLoader:
    def _argv(self, tmp_path: Path) -> tuple[list[str], Path]:
        spec = _spec(tmp_path, runner_options={"image": "img"})
        fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
        with patch("shutil.which", return_value="/usr/bin/docker"), \
             patch("subprocess.run", return_value=fail) as run:
            DockerRunner().run(spec)
        return [str(a) for a in run.call_args.args[0]], spec.capsule_dir

    def test_materialises_the_loader_into_the_mounted_capsule_dir(
        self, tmp_path: Path
    ) -> None:
        _argv, capsule_dir = self._argv(tmp_path)
        loader = capsule_dir / "sitecustomize.py"
        assert loader.exists(), (
            "B3 regression: no sitecustomize.py in the capsule dir, so the "
            "container's interpreter loads no hooks and model-calls.jsonl will "
            "come back empty while the capsule still reports success."
        )
        assert loader.read_text(encoding="utf-8") == HOOK_LOADER

    def test_puts_the_in_container_capsule_dir_on_pythonpath(
        self, tmp_path: Path
    ) -> None:
        argv, _ = self._argv(tmp_path)
        pythonpath = [a for a in argv if a.startswith("PYTHONPATH=")]
        assert pythonpath, (
            f"B3 regression: no PYTHONPATH passed to the container. "
            f"Materialising the loader is only half the fix. argv={argv}"
        )
        # It must point at the in-container mount point, not the host path.
        assert pythonpath[0].startswith("PYTHONPATH=/novafabric/capsule"), (
            f"PYTHONPATH must name the in-container path: {pythonpath[0]}"
        )

    def test_an_existing_pythonpath_is_preserved_not_replaced(
        self, tmp_path: Path
    ) -> None:
        spec = _spec(tmp_path, runner_options={"image": "img"})
        spec.env["PYTHONPATH"] = "/opt/workload-libs"
        fail = subprocess.CompletedProcess([], 1, b"", b"stop\n")
        with patch("shutil.which", return_value="/usr/bin/docker"), \
             patch("subprocess.run", return_value=fail) as run:
            DockerRunner().run(spec)
        argv = [str(a) for a in run.call_args.args[0]]
        pp = next(a for a in argv if a.startswith("PYTHONPATH="))
        assert pp == "PYTHONPATH=/novafabric/capsule:/opt/workload-libs", (
            f"prepend, never clobber, the workload's own PYTHONPATH: {pp}"
        )


class TestSlurmInjectsTheHookLoader:
    def test_slurm_wrap_script_sets_pythonpath(self, tmp_path: Path) -> None:
        from novafabric.runners._slurm import _build_wrap_script

        script = _build_wrap_script(
            ["python", "agent.py"],
            {"NOVAFABRIC_SPAN_ID": "a", "PATH": "/usr/bin"},
            tmp_path / "capsule",
        )
        assert "export PYTHONPATH=" in script
        assert str(tmp_path / "capsule") in script


@pytest.mark.xfail(
    strict=True,
    reason=(
        "B3, still open for the Kubernetes runner — see ADR-0272. Its capsule is "
        "an emptyDir, not a bind mount, and `kubectl cp` only runs after the "
        "workload exits, so neither the Docker nor the SLURM mechanism transfers. "
        "Injecting the loader needs either a ConfigMap (new RBAC beyond the "
        "documented jobs/pods/pods-log/pods-exec) or a shell command wrapper (a "
        "new assumption about the image). That is a decision, not a patch. "
        "strict=True so this flips to a failure the moment it is fixed."
    ),
)
def test_kubernetes_injects_the_hook_loader(tmp_path: Path) -> None:
    """Assert the pod SPEC actually carries the bootstrap.

    An earlier version of this test grepped the module source for the strings
    "sitecustomize" and "PYTHONPATH". Adding a *comment* that named them made it
    XPASS — it never tested behaviour at all. `strict=True` caught that. Assert
    on the applied manifest instead: what the cluster is told, not what the file
    says.
    """
    import json

    from novafabric.runners import KubernetesRunner

    apply_fail = subprocess.CompletedProcess([], 1, b"", b"stop here\n")
    with patch("subprocess.run", return_value=apply_fail) as run:
        KubernetesRunner().run(_spec(
            tmp_path, runner_options={"image": "img", "namespace": "ns"},
        ))
    manifest = json.loads(run.call_args.kwargs["input"].decode())
    container = manifest["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e.get("value", "") for e in container.get("env", [])}
    assert "PYTHONPATH" in env, (
        "the pod gets no PYTHONPATH, so its interpreter loads no hooks and "
        f"model-calls.jsonl comes back empty. container env: {sorted(env)}"
    )


class TestKubernetesSaysSoInsteadOfClaimingSuccess:
    """ADR-0272 option E: until the loader reaches the pod, the runner must not
    let an operator read `success` as `capture worked`."""

    def _result(self, tmp_path: Path):
        from novafabric.runners import KubernetesRunner

        # apply succeeds, then the job is reported complete, then kubectl cp
        # and delete are best-effort; a blanket success keeps the run on the
        # happy path so we observe what a *successful* run reports.
        ok = subprocess.CompletedProcess([], 0, b'{"status":{"succeeded":1}}', b"")
        with patch("subprocess.run", return_value=ok), \
             patch("novafabric.runners._kubernetes.jittered_sleep", lambda *_a, **_k: None):
            return KubernetesRunner().run(_spec(
                tmp_path, runner_options={"image": "img", "namespace": "ns"},
            ))

    def test_the_warning_names_the_missing_evidence(self, tmp_path: Path) -> None:
        result = self._result(tmp_path)
        stderr = result.stderr.decode()
        assert "wire-level capture did not run" in stderr, (
            "a successful-looking k8s run must say that model calls were not "
            f"recorded; stderr was: {stderr!r}"
        )
        assert "model-calls.jsonl" in stderr
        assert "ADR-0272" in stderr

    def test_runner_metadata_records_the_gap_machine_readably(
        self, tmp_path: Path
    ) -> None:
        result = self._result(tmp_path)
        assert result.runner_metadata.get("wire_capture") == "unavailable", (
            "the gap must be machine-readable in runner_metadata, not only prose "
            f"on stderr: {result.runner_metadata}"
        )
