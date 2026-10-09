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


def host_info() -> dict[str, Any]:
    """The ``capsule.yaml:host`` block, measured — the one builder every capsule
    writer uses (the main capture path, the framework adapters and the SDK agent).

    Adapter and SDK-agent capsules used to write ``cpu_count: 1`` and
    ``memory_bytes: 0`` whatever the machine; ``tests/capture/
    test_host_arch_is_never_hardcoded.py`` now fails on any literal for a measured
    host field.
    """
    try:
        cpu_count = os.cpu_count() or 1
    except Exception:
        cpu_count = 1
    return {
        "os": _os_name(),
        "arch": host_arch(),
        "python": platform.python_version(),
        "cpu_count": cpu_count,
        "memory_bytes": _memory_bytes(),
        "gpu": [],
        "hostname_redacted": True,
    }


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
