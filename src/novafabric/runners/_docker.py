"""Docker runner (ADR-0025 Runners.2).

Spawns the captured workload inside a container via ``docker run``,
mounts the capsule directory as a volume, and forwards stdout/stderr
back to the orchestrator. The container's exit code is the workload's
exit code.

Image responsibility (per ADR-0025): the user supplies an image that
already has NovaFabric installed (typically built from a Dockerfile
that does ``pip install novafabric``). Without NovaFabric inside the
container, the sitecustomize loader cannot import
``novafabric.capture.capsule.CapsuleWriter`` and capture hooks will
fail to install. We do not pick a default image — that would couple
NovaFabric to a registry and a maintained base image, which is out
of scope for v0.6.

Anti-patterns explicitly forbidden by this runner (per ADR-0025
§Anti-patterns and CLAUDE.md):
  - No privileged containers (``--privileged``).
  - No host-PID / host-network / host-IPC namespaces by default.
  - No mounting the docker socket into the container.
  - No hardcoded image registries — image must be passed via
    ``runner_options['image']`` or ``.novafabric/runners.yaml``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from novafabric.runners._options import coerce_str_dict, coerce_str_list
from novafabric.runners._types import ContainerEvalError, RunnerJobResult, RunnerJobSpec

# Both accept the CLI's string form as well as a config file's real list/dict —
# see novafabric.runners._options for why that distinction was silently losing
# every structured option passed with --runner-option.
_coerce_str_dict = coerce_str_dict
_coerce_str_list = coerce_str_list


class DockerRunner:
    """Run the captured workload inside a Docker container.

    Required ``runner_options``:

    - ``image`` (str) — fully-qualified image reference (e.g.
      ``myorg/agent-runtime:abc1234``).

    Optional ``runner_options``:

    - ``network`` (str) — passed as ``--network <value>``. Defaults
      to docker's default (bridge). ``host`` is allowed but discouraged.
    - ``workdir`` (str) — set the container's working directory. If
      unset, uses the image's default ``WORKDIR``.
    - ``extra_volumes`` (list[str]) — additional ``-v`` mounts beyond
      the capsule volume. Each entry is the raw ``host:container[:opts]``
      string passed straight to docker.
    - ``extra_env`` (dict[str, str]) — additional env vars layered
      on top of the orchestrator-provided env.
    - ``user`` (str) — passed as ``--user <value>``. Encouraged (e.g.
      ``1000:1000``) to avoid root-owned files in the mounted capsule.
    """

    name: str = "docker"
    description: str = (
        "Run the captured workload inside a Docker container. "
        "Requires runner_options['image']."
    )

    def __init__(self, *, docker_bin: str = "docker") -> None:
        # Override-friendly for testing (mock the binary or point at a
        # rootless docker like podman if a user wants to).
        self._docker = docker_bin

    def supports(self) -> tuple[bool, str]:
        if shutil.which(self._docker) is None:
            return False, f"`{self._docker}` binary not found on PATH"
        # `docker info` requires daemon connectivity. Cheaper than `docker ps`.
        try:
            proc = subprocess.run(
                [self._docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True, timeout=5,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, f"could not probe docker daemon: {exc}"
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace").strip().splitlines()
            tail = err[-1] if err else "(no stderr)"
            return False, f"docker daemon not reachable: {tail}"
        return True, ""

    def run(self, spec: RunnerJobSpec) -> RunnerJobResult:
        opts = spec.runner_options or {}
        image = opts.get("image") if isinstance(opts.get("image"), str) else None
        if not image:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                runner_error=(
                    "DockerRunner requires runner_options['image']. "
                    "Pass --runner-option image=<ref> or set image: in "
                    ".novafabric/runners.yaml"
                ),
            )

        # In-container capsule path. The orchestrator's host capsule_dir is
        # mounted at this path, so NOVAFABRIC_CAPSULE_DIR inside the
        # container resolves to writable space that the orchestrator will
        # see on the host after the container exits.
        in_container_capsule = "/novafabric/capsule"

        # Env: start from the orchestrator-provided env, then override the
        # capsule path so it's container-relative. Then layer extra_env.
        env = dict(spec.env)
        env["NOVAFABRIC_CAPSULE_DIR"] = in_container_capsule
        for k, v in _coerce_str_dict(opts.get("extra_env")).items():
            env[k] = v

        # docker run argv. Order matters: --rm, env, volumes, image-level
        # options, then image, then command.
        argv: list[str] = [self._docker, "run", "--rm"]

        # Each env var as -e KEY=VALUE. Filter to NOVAFABRIC_* + extra_env
        # to avoid leaking arbitrary host env (PATH, HOME, secrets, etc.)
        # into the container — docker would otherwise inherit nothing,
        # but the orchestrator passes its full env in spec.env.
        for key, value in env.items():
            if key.startswith("NOVAFABRIC_") or key in _coerce_str_dict(
                opts.get("extra_env")
            ):
                argv.extend(["-e", f"{key}={value}"])

        # Capsule volume — host:container, rw.
        argv.extend(["-v", f"{spec.capsule_dir}:{in_container_capsule}"])

        # Optional: extra volumes, network, workdir, user.
        for v in _coerce_str_list(opts.get("extra_volumes")):
            argv.extend(["-v", v])

        network = opts.get("network")
        if isinstance(network, str) and network:
            argv.extend(["--network", network])

        workdir = opts.get("workdir")
        if isinstance(workdir, str) and workdir:
            argv.extend(["--workdir", workdir])

        user = opts.get("user")
        if isinstance(user, str) and user:
            argv.extend(["--user", user])

        # Hard-stop deadline: docker run waits indefinitely without one.
        timeout = spec.timeout_s if spec.timeout_s is not None else 600.0

        argv.append(image)
        argv.extend(spec.command)

        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            return RunnerJobResult(
                exit_code=124,
                runner_status="timeout",
                stdout=exc.stdout or b"",
                stderr=(exc.stderr or b"") + b"[novafabric] docker capture timeout",
                runner_error=f"workload exceeded {timeout}s wall-clock deadline",
                runner_metadata={"image": image},
            )
        except FileNotFoundError as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                stderr=str(exc).encode(),
                runner_error=str(exc),
                runner_metadata={"image": image},
            )
        except Exception as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                stderr=str(exc).encode(),
                runner_error=str(exc),
                runner_metadata={"image": image},
            )

        # docker exit codes:
        #   125 = docker daemon error / image pull failure / bad arg
        #   126 = container command not executable
        #   127 = container command not found
        #   else = workload exit code
        runner_status = "completed"
        runner_error: str | None = None
        if proc.returncode == 125:
            runner_status = "failed_setup"
            runner_error = (
                "docker run reported a setup failure (image pull, "
                "daemon error, or bad argument). See stderr."
            )

        return RunnerJobResult(
            exit_code=proc.returncode,
            runner_status=runner_status,
            stdout=proc.stdout,
            stderr=proc.stderr,
            runner_error=runner_error,
            runner_metadata={"image": image},
        )

    # ── Eval container helper (ADR-0033 E-4) ──────────────────────────────────

    _EVAL_CAPSULE_MOUNTPOINT = "/novafabric/eval-capsule"

    def run_eval_container(
        self,
        *,
        image_ref: str,
        digest: str,
        capsule_path: Path,
        command: list[str],
        env: dict[str, str] | None = None,
        timeout_s: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run an OCI-pinned eval container with *capsule_path* mounted read-only.

        The capsule is available inside the container at
        ``/novafabric/eval-capsule`` (read-only).  The container's stdout is
        the primary output channel — suite adapters should write their
        ``EvalResult`` JSON to stdout.

        Parameters
        ----------
        image_ref:
            Registry image name, e.g. ``ghcr.io/org/image:tag``.
        digest:
            ``sha256:<hex>`` digest of the pinned image.  Docker uses
            ``image_ref@digest`` as the image reference, which guarantees
            the exact image layer graph is used.  Must start with ``sha256:``.
        capsule_path:
            Host path to the capsule directory, mounted read-only.
        command:
            Argv to execute inside the container.
        env:
            Extra env vars injected as ``-e KEY=VALUE``.
        timeout_s:
            Wall-clock deadline in seconds.  Defaults to 600.

        Returns
        -------
        tuple[int, bytes, bytes]
            ``(exit_code, stdout, stderr)``

        Raises
        ------
        ContainerEvalError
            If the digest format is invalid, the Docker daemon is not
            reachable, or Docker reports a setup failure (exit code 125).
        """
        if not digest.startswith("sha256:"):
            raise ContainerEvalError(
                f"invalid OCI digest {digest!r}: must start with 'sha256:'"
            )

        ok, reason = self.supports()
        if not ok:
            raise ContainerEvalError(f"Docker not available: {reason}")

        # image_ref@digest pins the exact layer graph; Docker will pull if the
        # digest is not in local cache and refuse if the resolved digest differs.
        pinned = f"{image_ref}@{digest}"
        mount = f"{capsule_path}:{self._EVAL_CAPSULE_MOUNTPOINT}:ro"

        argv: list[str] = [self._docker, "run", "--rm", "-v", mount]
        for key, value in (env or {}).items():
            argv.extend(["-e", f"{key}={value}"])
        argv.append(pinned)
        argv.extend(command)

        timeout = timeout_s if timeout_s is not None else 600.0

        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ContainerEvalError(
                f"eval container exceeded {timeout}s wall-clock deadline"
            ) from exc
        except (FileNotFoundError, OSError) as exc:
            raise ContainerEvalError(f"could not launch Docker: {exc}") from exc

        if proc.returncode == 125:
            stderr_text = (proc.stderr or b"").decode(errors="replace").strip()
            raise ContainerEvalError(
                f"Docker setup failure (digest mismatch or bad argument): {stderr_text}"
            )

        return proc.returncode, proc.stdout, proc.stderr
