from __future__ import annotations

import hashlib
import importlib.metadata
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from novafabric import __version__

_DEFAULT_DENY_PATTERNS = [
    r"(?i).*api[_-]?key.*",
    r"(?i).*secret.*",
    r"(?i).*token.*",
    r"(?i).*password.*",
    r"(?i).*auth.*",
    r"(?i).*credential.*",
]

_ALLOW_PREFIXES = (
    "LANG", "LC_", "TZ", "PATH", "VIRTUAL_ENV",
    "PYTHONPATH", "HOME", "USER", "SHELL",
)


def _digest(value: str) -> str:
    """The bare hex digest, with no algorithm prefix."""
    return hashlib.sha256(value.encode()).hexdigest()


def _stable_hash(value: str) -> str:
    return "sha256:" + _digest(value)


def _detect_package_manager(cwd: Path) -> tuple[str, str]:
    for lock_file, manager in [
        ("uv.lock", "uv"),
        ("poetry.lock", "poetry"),
        ("pdm.lock", "pdm"),
        ("requirements.txt", "pip"),
    ]:
        if (cwd / lock_file).exists():
            return manager, lock_file
    return "pip", "none"


def _installed_packages() -> list[dict[str, Any]]:
    pkgs: list[dict[str, Any]] = []
    try:
        for dist in importlib.metadata.distributions():
            name = dist.metadata.get("Name", "")
            version = dist.metadata.get("Version", "")
            if name:
                pkgs.append({
                    "name": name, "version": version or "unknown", "source": "pypi",
                })
    except Exception:
        pass
    return pkgs[:200]


def _runtime_env() -> dict[str, Any]:
    deny_patterns = [re.compile(p) for p in _DEFAULT_DENY_PATTERNS]
    recorded: dict[str, str] = {}
    dropped_count = 0
    dropped_hashed: list[str] = []

    for key, val in os.environ.items():
        if any(p.match(key) for p in deny_patterns):
            dropped_count += 1
            dropped_hashed.append(_stable_hash(key))
        elif any(key.startswith(pfx) for pfx in _ALLOW_PREFIXES):
            recorded[key] = val

    return {
        "policy": "default",
        "allow_list": list(_ALLOW_PREFIXES),
        "deny_list": _DEFAULT_DENY_PATTERNS,
        "recorded": recorded,
        "dropped_count": dropped_count,
        "dropped_keys_hashed": dropped_hashed,
    }


def _linux_memory_bytes() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


def _darwin_memory_bytes() -> int:
    try:
        proc = subprocess.run(
            ["sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=2, check=False,
        )
        if proc.returncode != 0:
            return 0
        value = int(proc.stdout.strip())
    except Exception:
        return 0
    return value if value > 0 else 0


def _windows_memory_bytes() -> int:
    try:
        import ctypes

        class _MemoryStatusEx(ctypes.Structure):
            _fields_ = [  # noqa: RUF012 - ctypes reads this class attribute
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        # getattr: ``ctypes.windll`` exists only on Windows (and mypy knows it).
        kernel32 = getattr(ctypes, "windll").kernel32  # noqa: B009
        if not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return 0
        return int(stat.ullTotalPhys)
    except Exception:
        return 0


def _memory_bytes() -> int:
    """Total physical memory in bytes, best-effort and stdlib-only; ``0`` = unknown.

    ``host.memory_bytes`` is a required non-negative integer in both the capsule and
    the env.lock schema, with no null, so ``0`` is the only way to record "could not
    be measured" — it never means a machine with no memory. Each OS the schema
    names has its own reader; a failure in any of them degrades to ``0``, never to
    an exception that would fail the capture.
    """
    if sys.platform == "darwin":
        return _darwin_memory_bytes()
    if sys.platform.startswith("win"):
        return _windows_memory_bytes()
    return _linux_memory_bytes()


def host_arch() -> str:
    """The host CPU architecture as every capsule writer records it.

    The single normalisation for ``capsule.yaml:host.arch`` and
    ``env.lock:host.arch`` (``aarch64`` -> ``arm64``, ``amd64`` -> ``x86_64``).
    Capsule writers call this; none may hardcode an architecture —
    ``tests/capture/test_host_arch_is_never_hardcoded.py`` fails if one does.
    """
    machine = platform.machine().lower()
    arch_map = {
        "x86_64": "x86_64", "amd64": "x86_64",
        "arm64": "arm64", "aarch64": "arm64",
        "riscv64": "riscv64", "s390x": "s390x", "ppc64le": "ppc64le",
    }
    # An unmapped machine string is recorded as reported, never guessed: a
    # default of "x86_64" would be false evidence on any other host.
    return arch_map.get(machine, machine or "unknown")


def host_info(*, scheduler_context: bool = True) -> dict[str, Any]:
    """The ``capsule.yaml:host`` block, measured — the one builder every capsule
    writer uses (the main capture path, the framework adapters and the SDK agent).

    Adapter and SDK-agent capsules used to write ``cpu_count: 1`` and
    ``memory_bytes: 0`` whatever the machine; ``tests/capture/
    test_host_arch_is_never_hardcoded.py`` now fails on any literal for a measured
    host field.

    ``scheduler_context`` (ADR-0307) adds ``host.slurm`` when this process runs
    inside a Slurm job. Pass ``False`` from a writer whose capsule describes a
    run that happened somewhere else (the OTLP importer): this process's job is
    not that run's job.
    """
    try:
        cpu_count = os.cpu_count() or 1
    except Exception:
        cpu_count = 1
    host: dict[str, Any] = {
        "os": _os_name(),
        "arch": host_arch(),
        "python": platform.python_version(),
        "cpu_count": cpu_count,
        "memory_bytes": _memory_bytes(),
        "gpu": [],
        "hostname_redacted": True,
    }
    if scheduler_context:
        slurm = slurm_context_from_env()
        if slurm is not None:
            host["slurm"] = slurm
    return host


# ── Scheduler and runner context (ADR-0307) ──────────────────────────────────
#
# Explicit allow-list: these Slurm variables, and no others, may reach the
# manifest. Never the full environment, never SLURM_* by prefix — a site can
# export anything under that prefix (SLURM_JOB_ACCOUNT, SLURM_SUBMIT_DIR, a
# wrapper's own SLURM_* secrets), and a prefix rule would carry all of it.
_SLURM_ID_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("job_id", ("SLURM_JOB_ID", "SLURM_JOBID")),  # SLURM_JOBID: older releases
    ("array_job_id", ("SLURM_ARRAY_JOB_ID",)),
    ("array_task_id", ("SLURM_ARRAY_TASK_ID",)),
)
_SLURM_NAME_FIELDS: tuple[tuple[str, str], ...] = (
    ("partition", "SLURM_JOB_PARTITION"),
    ("cluster", "SLURM_CLUSTER_NAME"),
)
_SLURM_NODE_LIST_VARS = ("SLURM_JOB_NODELIST", "SLURM_NODELIST")
_SLURM_NODE_COUNT_VARS = ("SLURM_JOB_NUM_NODES", "SLURM_NNODES")

#: Every variable :func:`slurm_context_from_env` reads — the whole allow-list.
SLURM_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {var for _, names in _SLURM_ID_FIELDS for var in names}
    | {var for _, var in _SLURM_NAME_FIELDS}
    | set(_SLURM_NODE_LIST_VARS)
    | set(_SLURM_NODE_COUNT_VARS)
)

_SLURM_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _first(environ: Any, names: tuple[str, ...]) -> str:
    for name in names:
        value = (environ.get(name) or "").strip()
        if value:
            return value
    return ""


def node_list_hash(node_list: str) -> str:
    """``sha256:<16 hex>`` of a Slurm node list, as ``env.lock`` hashes the hostname.

    Node names are hostnames, and the manifest never carries a hostname
    (``host.hostname_redacted: true``). The digest still answers "did this job
    run on ``node[01-04]``?" for anyone who knows the expression.
    """
    return f"sha256:{_digest(node_list)[:16]}"


def slurm_context_from_env(environ: Any = None) -> dict[str, Any] | None:
    """``host.slurm`` from the allow-listed ``SLURM_*`` variables, or ``None``.

    ``None`` unless ``SLURM_JOB_ID`` (or ``SLURM_JOBID``) holds a job id, so a
    machine with no scheduler is unchanged. A malformed value is left out rather
    than repaired: ids must be decimal, names must look like Slurm names.
    """
    env = os.environ if environ is None else environ
    block: dict[str, Any] = {}
    for key, names in _SLURM_ID_FIELDS:
        value = _first(env, names)
        if value.isdigit():
            block[key] = value
    if "job_id" not in block:
        return None
    for key, var in _SLURM_NAME_FIELDS:
        value = _first(env, (var,))
        if _SLURM_NAME_RE.match(value):
            block[key] = value
    node_list = _first(env, _SLURM_NODE_LIST_VARS)
    if node_list:
        block["node_list_hash"] = node_list_hash(node_list)
    node_count = _first(env, _SLURM_NODE_COUNT_VARS)
    if node_count.isdigit() and int(node_count) >= 1:
        block["node_count"] = int(node_count)
    block["source"] = "environment"
    return block


def slurm_context_from_runner(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """``host.slurm`` for ``--runner slurm``, from what ``sbatch`` reported.

    The capturing process runs on the submit node, so its own environment says
    nothing about the job; the runner's metadata does. ``partition`` is recorded
    only when one partition was requested — a ``gpu,cpu`` request does not say
    which one the job ran in.
    """
    job_id = str(metadata.get("job_id") or "").strip()
    if not job_id.isdigit():
        return None
    block: dict[str, Any] = {"job_id": job_id}
    partition = str(metadata.get("partition") or "").strip()
    if _SLURM_NAME_RE.match(partition):
        block["partition"] = partition
    cluster = str(metadata.get("cluster") or "").strip()
    if _SLURM_NAME_RE.match(cluster):
        block["cluster"] = cluster
    block["source"] = "runner"
    return block


def runner_context(name: str, metadata: dict[str, Any]) -> dict[str, Any]:
    """``host.runner``: which runner ran the workload, and for a container runner
    the image the runtime resolved (ADR-0307).

    Only the runner name and the ``image_provenance`` block cross into the
    manifest. The rest of ``runner_metadata`` (job names, pod names, sacct exit
    strings) stays out: it is unvalidated, runner-specific and not evidence.
    """
    from novafabric.runners._image import IMAGE_PROVENANCE_KEY

    block: dict[str, Any] = {"name": name}
    image = metadata.get(IMAGE_PROVENANCE_KEY)
    if isinstance(image, dict) and image.get("reference"):
        block["image"] = dict(image)
    return block


def _os_name() -> str:
    """``host.os`` for both the capsule and the env.lock host block.

    Known gap: both schemas close ``host.os`` to ``linux|darwin|windows``, so any
    other OS (FreeBSD, AIX, ...) is recorded as ``"linux"``. Adding an ``"other"``
    value would make an older ``nova validate`` reject new capsules, so the enum
    is unchanged until a schema-evolution decision covers it.
    """
    name = platform.system().lower()
    return name if name in ("linux", "darwin", "windows") else "linux"


def _inference_facet() -> dict[str, Any]:
    """LLM inference numerical-determinism params from the NOVAFABRIC_INFERENCE_* contract.

    Best-effort: only keys present in the environment are recorded; malformed
    integers are dropped rather than raised. Returns an empty dict when nothing is
    set, so the optional ``hardware.inference`` block is omitted entirely
    (backward-compatible). Records the signal (tensor-parallel size, dtype, batch
    size, engine) that separates environmental drift from genuine behavioral
    regression during replay/diff (SOTA sweep src-402/403/416).
    """
    prefix = "NOVAFABRIC_INFERENCE_"
    str_fields = {
        "ENGINE": "engine",
        "ENGINE_VERSION": "engine_version",
        "DTYPE": "dtype",
        "ATTENTION_BACKEND": "attention_backend",
    }
    int_fields = {
        "TP_SIZE": "tensor_parallel_size",
        "PP_SIZE": "pipeline_parallel_size",
        "BATCH_SIZE": "batch_size",
        "SEED": "seed",
    }
    facet: dict[str, Any] = {}
    for suffix, key in str_fields.items():
        val = os.environ.get(prefix + suffix)
        if val:
            facet[key] = val
    for suffix, key in int_fields.items():
        val = os.environ.get(prefix + suffix)
        if val is None:
            continue
        try:
            facet[key] = int(val)
        except ValueError:
            pass  # malformed int degrades to omission, never crashes capture
    det = os.environ.get(prefix + "DETERMINISTIC")
    if det is not None:
        facet["deterministic"] = det.strip().lower() in ("1", "true", "yes", "on")
    return facet


def capture_environment(created_at: str, run_id: str) -> dict[str, Any]:
    import socket

    cwd = Path.cwd()
    pkg_manager, lock_kind = _detect_package_manager(cwd)

    best_effort_reasons: list[dict[str, str]] = []
    if lock_kind == "none":
        best_effort_reasons.append(
            {"kind": "missing_lock_file",
             "detail": "No recognised lock file in working directory"}
        )

    lock_file_hash: str | None = None
    if lock_kind != "none":
        lock_path = cwd / lock_kind
        if lock_path.exists():
            content = lock_path.read_bytes()[:65536]
            lock_file_hash = _stable_hash(content.decode("utf-8", errors="replace"))
        else:
            lock_kind = "none"
            lock_file_hash = None
            if not best_effort_reasons:
                best_effort_reasons.append(
                    {"kind": "missing_lock_file",
                     "detail": f"{lock_kind} declared but not found"}
                )

    hostname_hash = _digest(socket.gethostname())[:16]
    lang = os.environ.get("LANG", "en_US.UTF-8")
    tz = os.environ.get("TZ", "") or "UTC"

    try:
        cpu_count = os.cpu_count() or 1
    except Exception:
        cpu_count = 1

    py_ver = platform.python_version()
    exe_path = sys.executable.replace(str(Path.home()), "~")
    impl = platform.python_implementation().lower()
    interpreter = "cpython" if impl == "cpython" else "pypy3"

    mode = "best-effort" if best_effort_reasons else "deterministic"

    lock: dict[str, Any] = {
        "schema_version": "0.1.0",
        "mode": mode,
        "host": {
            "os": _os_name(),
            "arch": host_arch(),
            "hostname": f"sha256:{hostname_hash}",
            "cpu_count": cpu_count,
            "memory_bytes": _memory_bytes(),
        },
        "runtime": {
            "timezone": tz,
            "wallclock_at_start": created_at,
        },
        "python": {
            "interpreter": interpreter,
            "version": py_ver,
            "executable_path": exe_path,
            "package_manager": pkg_manager,
            "lock_file_kind": lock_kind,
            "lock_file_ref": None,
            "lock_file_hash": lock_file_hash,
            "installed_packages": _installed_packages(),
        },
        "hardware": {
            "gpus": [],
            "cuda_version": None,
            "cudnn_version": None,
            "nccl_version": None,
            "rocm_version": None,
        },
        "runtime_env": _runtime_env(),
        "locale": {
            "lang": lang,
            "lc_all": os.environ.get("LC_ALL"),
            "tz": tz,
        },
        "captured_at": created_at,
        "captured_by": f"novafabric/{__version__}",
    }
    inference = _inference_facet()
    if inference:
        lock["hardware"]["inference"] = inference

    if best_effort_reasons:
        lock["best_effort_reasons"] = best_effort_reasons
    return lock
