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
§Anti-patterns and the project anti-pattern rules):
  - No privileged containers (``--privileged``).
  - No host-PID / host-network / host-IPC namespaces by default.
  - No mounting the docker socket into the container.
  - No hardcoded image registries — image must be passed via
    ``runner_options['image']`` or ``.novafabric/runners.yaml``.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from novafabric.runners._env import forwardable_env
from novafabric.runners._image import (
    DOCKER_INSPECT_FORMAT,
    IMAGE_PROVENANCE_KEY,
    ImageInspection,
    image_block,
    parse_docker_inspect,
    resolve_docker_image,
)
from novafabric.runners._options import coerce_str_dict, coerce_str_list
from novafabric.runners._sitecustomize import HOOK_LOADER as _HOOK_LOADER
from novafabric.runners._types import (
    ContainerEvalError,
    RunnerJobResult,
    RunnerJobSpec,
    attach_interrupted_result,
    interrupted_job_result,
)

logger = logging.getLogger(__name__)

# Both accept the CLI's string form as well as a config file's real list/dict —
# see novafabric.runners._options for why that distinction was silently losing
# every structured option passed with --runner-option.
_coerce_str_dict = coerce_str_dict
_coerce_str_list = coerce_str_list

#: Variables the ``docker`` CLI itself -- or a credential helper / CLI plugin it
#: spawns -- reads from its own environment: which daemon and context to use,
#: where its config lives, which binaries ``PATH`` finds, the proxy it dials
#: through. A container value for one of these cannot ride in the client's
#: environment without changing what the client does, so it is the one case
#: still passed as ``-e NAME=VALUE`` (on the command line). None of them is a
#: place for a credential.
_DOCKER_CLIENT_KEYS = frozenset({
    "PATH", "HOME", "USER", "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME",
    "SSH_AUTH_SOCK", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
})
_DOCKER_CLIENT_PREFIXES = ("DOCKER_", "BUILDKIT_", "LD_")


def _by_name_only(key: str) -> bool:
    """Whether *key*'s value can travel in the docker client's environment."""
    if not key or "=" in key or "\x00" in key:
        return False  # not a name the OS accepts in an environment: docker parses it
    return key not in _DOCKER_CLIENT_KEYS and not key.startswith(_DOCKER_CLIENT_PREFIXES)


def docker_env_args(
    container_env: Mapping[str, str], client_base: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """The ``-e`` flags for *container_env* and the environment to run ``docker`` with.

    A value must never be on ``docker run``'s command line: argv is
    world-readable on the host (``ps``, ``/proc/<pid>/cmdline``) for as long as
    the container runs, and ``extra_env`` is how operators pass API keys. So
    each variable is named with ``-e NAME`` -- docker then copies the value from
    its *own* environment -- and the value is put in the environment of the
    ``docker`` client process, which only its user and root can read
    (``/proc/<pid>/environ`` is mode 0400). The container receives exactly the
    same ``NAME=VALUE`` pairs as before.

    Exception: a name in :data:`_DOCKER_CLIENT_KEYS` or starting with one of
    ``_DOCKER_CLIENT_PREFIXES`` (``PATH``, ``HOME``, ``DOCKER_*``, proxies...)
    steers the client itself, so it keeps the literal ``-e NAME=VALUE`` form.

    Args:
        container_env: the variables the container must receive (already
            filtered by :func:`~novafabric.runners._env.forwardable_env`).
        client_base: the environment the ``docker`` CLI would otherwise
            inherit (``os.environ``); the client keeps all of it.

    Returns:
        ``(flags, client_env)``. Never mutates its inputs.
    """
    flags: list[str] = []
    client_env = dict(client_base)
    for key, value in container_env.items():
        if _by_name_only(key):
            flags.extend(["-e", key])
            client_env[key] = value
        else:
            flags.extend(["-e", f"{key}={value}"])
    return flags, client_env


#: Where :meth:`DockerRunner.run_eval_container` mounts the capsule (read-only)
#: inside an eval container — the path a suite passes to its container binary.
EVAL_CAPSULE_MOUNTPOINT = "/novafabric/eval-capsule"


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
    - ``extra_env`` (dict[str, str]) — additional env vars for the
      container. Passed as ``docker run -e NAME`` with the value in the
      ``docker`` client's environment, so no value is in any process's
      argv (``ps``). It is still in the container's config
      (``docker inspect``), readable by anyone who can reach the daemon.
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

    def _remove_container(self, name: str, client_env: dict[str, str]) -> dict[str, Any]:
        """``docker rm -f <name>`` after a timeout; the outcome, for ``runner_metadata``."""
        try:
            subprocess.run(
                [self._docker, "rm", "-f", name],
                capture_output=True, check=True, timeout=30, env=client_env,
            )
        except Exception as exc:  # noqa: BLE001 — the timeout result must still be returned
            logger.warning("could not remove container %s after its deadline: %s", name, exc)
            return {"cancelled": False, "cancel_error": f"{type(exc).__name__}: {exc}"[:300]}
        return {"cancelled": True, "container": name}

    def _inspect_image(self, image: str) -> ImageInspection:
        """What the local runtime says *image* resolves to right now (ADR-0307).

        Never raises: a missing binary, an unknown image, a timeout or a
        malformed reply all come back as an ``ImageInspection`` with ``error``
        set, and the capsule then records the digest as absent with that reason.
        """
        try:
            proc = subprocess.run(
                [self._docker, "image", "inspect", "--format",
                 DOCKER_INSPECT_FORMAT, image],
                capture_output=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return ImageInspection(error=f"docker image inspect failed: {exc}")
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace").strip().splitlines()
            tail = err[-1] if err else f"exit {proc.returncode}"
            return ImageInspection(error=f"docker image inspect failed: {tail[:200]}")
        return parse_docker_inspect((proc.stdout or b"").decode(errors="replace"))

    def _metadata(
        self, image: str, before: ImageInspection, *, ran: bool
    ) -> dict[str, Any]:
        """``runner_metadata`` for a run of *image*, with the resolved digest.

        ``ran`` is False when ``docker run`` itself could not be launched; the
        before-run read then stands alone (nothing was pulled in between).
        """
        after = self._inspect_image(image) if ran else ImageInspection(
            error="docker run could not be launched"
        )
        return {
            "image": image,
            IMAGE_PROVENANCE_KEY: image_block(image, resolve_docker_image(before, after)),
        }

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
        try:
            extra_env = _coerce_str_dict(opts.get("extra_env"), name="extra_env")
        except ValueError as exc:
            return RunnerJobResult(
                exit_code=127, runner_status="failed_setup", runner_error=str(exc)
            )

        # In-container capsule path. The orchestrator's host capsule_dir is
        # mounted at this path, so NOVAFABRIC_CAPSULE_DIR inside the
        # container resolves to writable space that the orchestrator will
        # see on the host after the container exits.
        in_container_capsule = "/novafabric/capsule"

        # Materialize the sitecustomize hook loader into the capsule dir,
        # which is bind-mounted into the container below. Without it the
        # workload's interpreter never loads NovaFabric's hooks, so
        # `model-calls.jsonl` comes back EMPTY while the capsule still reports
        # `status: success` -- silent, total loss of the primary evidence
        # (defect B3; the same omission the SLURM runner fixed in v0.6.11).
        (spec.capsule_dir / "sitecustomize.py").write_text(
            _HOOK_LOADER, encoding="utf-8"
        )

        # Env: start from the orchestrator-provided env, then override the
        # capsule path so it's container-relative. Then layer extra_env.
        env = dict(spec.env)
        env["NOVAFABRIC_CAPSULE_DIR"] = in_container_capsule
        # Prepend the in-container capsule dir to PYTHONPATH so the loader
        # above auto-loads at interpreter startup. This is the half that makes
        # wire-level capture actually fire; preserve any PYTHONPATH the
        # orchestrator passed through.
        _existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{in_container_capsule}:{_existing_pp}"
            if _existing_pp
            else in_container_capsule
        )
        env.update(extra_env)

        # docker run argv. Order matters: --rm, env, volumes, image-level
        # options, then image, then command.
        argv: list[str] = [self._docker, "run", "--rm"]
        # A name the runner can address the container by: killing the `docker run`
        # client on timeout does not stop the container (capture-runners-04).
        container = f"novafabric-{spec.run_id.lower()}"
        argv.extend(["--name", container])

        # Default-deny (ADR-0270): NOVAFABRIC_* plus the operator's explicit
        # extra_env. The orchestrator passes its full env in spec.env, and
        # `docker inspect` exposes whatever we set. PYTHONPATH is allow-listed
        # deliberately: it is set by this runner (above), not inherited from
        # the submitter, and capture does not fire without it. It is not the
        # submitting shell's PYTHONPATH unless the orchestrator passed one,
        # which is then prefixed rather than replaced.
        # Each variable goes in as `-e NAME`, its value in the docker client's
        # own environment -- never `-e NAME=VALUE`, which put every extra_env
        # secret in argv, readable by any user with `ps` for the container's
        # whole lifetime (docker_env_args).
        env_flags, client_env = docker_env_args(
            forwardable_env(
                env,
                also_allow={"PYTHONPATH", *extra_env},
            ),
            os.environ,
        )
        argv.extend(env_flags)

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

        # ADR-0307: resolve the tag before the run (what a local image already
        # resolves to is what `docker run` uses) and again after it (what it
        # pulled, when nothing was local). Never trust the tag itself.
        before = self._inspect_image(image)

        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=timeout, env=client_env,
            )
        except (KeyboardInterrupt, SystemExit) as exc:
            # Ctrl-C / a forwarded SIGTERM killed the client, not the container
            # (review capture-runners-03): remove it, then let the orchestrator
            # finalise the capsule — only once the container is gone.
            try:
                removed = self._remove_container(container, client_env)
            except (KeyboardInterrupt, SystemExit):
                raise exc from None
            if removed["cancelled"]:
                attach_interrupted_result(exc, interrupted_job_result(
                    exc, runner_metadata={**self._metadata(image, before, ran=True), **removed},
                ))
            raise
        except subprocess.TimeoutExpired as exc:
            # The client is gone, the container is not: remove it (kill + rm) before
            # the capsule is sealed, so it cannot keep writing into the mount.
            metadata = self._metadata(image, before, ran=True)
            metadata.update(self._remove_container(container, client_env))
            return RunnerJobResult(
                exit_code=124,
                runner_status="timeout",
                stdout=exc.stdout or b"",
                stderr=(exc.stderr or b"") + b"[novafabric] docker capture timeout",
                runner_error=f"workload exceeded {timeout}s wall-clock deadline",
                runner_metadata=metadata,
            )
        except FileNotFoundError as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                stderr=str(exc).encode(),
                runner_error=str(exc),
                runner_metadata=self._metadata(image, before, ran=False),
            )
        except Exception as exc:
            return RunnerJobResult(
                exit_code=127,
                runner_status="failed_setup",
                stderr=str(exc).encode(),
                runner_error=str(exc),
                runner_metadata=self._metadata(image, before, ran=False),
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
            runner_metadata=self._metadata(image, before, ran=True),
        )

    # ── Eval container helper (ADR-0033 E-4) ──────────────────────────────────

    _EVAL_CAPSULE_MOUNTPOINT = EVAL_CAPSULE_MOUNTPOINT

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
            Extra env vars for the container. Each is passed as ``-e KEY``
            with its value in the ``docker`` client's environment, never on
            the command line (see :func:`docker_env_args`).
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
        env_flags, client_env = docker_env_args(env or {}, os.environ)
        argv.extend(env_flags)
        argv.append(pinned)
        argv.extend(command)

        timeout = timeout_s if timeout_s is not None else 600.0

        try:
            proc = subprocess.run(
                argv, capture_output=True, timeout=timeout, env=client_env
            )
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
