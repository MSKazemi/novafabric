"""``host.memory_bytes`` is measured on every OS the schema names, or recorded as 0 = unknown.

Found 2026-10-09: ``_memory_bytes()`` read only ``/proc/meminfo``, so every capsule
written on macOS or Windows recorded ``memory_bytes: 0`` — indistinguishable from a
measurement. The schema (``run-capsule`` ``$defs/Host`` and ``environment``
``$defs/HostSection``) requires the field as an integer >= 0 with no null, so 0 is
the only encoding of "not measured"; the schema descriptions now say so, and the
reader is stdlib-only and best-effort per OS: ``/proc/meminfo`` on Linux,
``sysctl -n hw.memsize`` on macOS, ``GlobalMemoryStatusEx`` on Windows.

These run on Linux CI, so the macOS and Windows paths are exercised with
monkeypatched ``sys.platform``, ``subprocess.run`` and ``ctypes.windll``.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from novafabric.capture import env as capture_env

_ROOT = Path(__file__).resolve().parents[2]

_GIB_16 = 16 * 1024**3


def _fake_sysctl(stdout: str, returncode: int = 0) -> Any:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_macos_reads_hw_memsize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = _fake_sysctl(f"{_GIB_16}\n")
    monkeypatch.setattr(subprocess, "run", fake)

    assert capture_env._memory_bytes() == _GIB_16
    assert fake.calls == [["sysctl", "-n", "hw.memsize"]]


@pytest.mark.parametrize(
    ("stdout", "returncode"),
    [("", 1), ("not-a-number\n", 0), ("", 0), ("-5\n", 0)],
)
def test_macos_sysctl_failure_records_unknown_as_zero(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _fake_sysctl(stdout, returncode))
    assert capture_env._memory_bytes() == 0


def test_macos_missing_sysctl_records_unknown_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    def missing(cmd: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(subprocess, "run", missing)
    assert capture_env._memory_bytes() == 0


class _FakeKernel32:
    def __init__(self, total: int, ok: bool = True) -> None:
        self.total = total
        self.ok = ok

    def GlobalMemoryStatusEx(self, ref: Any) -> int:  # noqa: N802 - Win32 name
        stat = ref._obj
        assert stat.dwLength == ctypes.sizeof(stat), "dwLength must be set before the call"
        stat.ullTotalPhys = self.total
        return 1 if self.ok else 0


class _FakeWinDLL:
    def __init__(self, kernel32: _FakeKernel32) -> None:
        self.kernel32 = kernel32


def test_windows_reads_global_memory_status_ex(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", _FakeWinDLL(_FakeKernel32(_GIB_16)), raising=False)
    assert capture_env._memory_bytes() == _GIB_16


def test_windows_api_failure_records_unknown_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        ctypes, "windll", _FakeWinDLL(_FakeKernel32(_GIB_16, ok=False)), raising=False
    )
    assert capture_env._memory_bytes() == 0


def test_windows_without_windll_records_unknown_as_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(ctypes, "windll", raising=False)
    assert capture_env._memory_bytes() == 0


@pytest.mark.skipif(not Path("/proc/meminfo").exists(), reason="needs Linux /proc")
def test_linux_still_reads_proc_meminfo(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert capture_env._memory_bytes() > 0


def test_host_info_carries_the_measured_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "run", _fake_sysctl(f"{_GIB_16}\n"))
    assert capture_env.host_info()["memory_bytes"] == _GIB_16


#: Packaged (in force) and top-level ``schemas/`` (OAS v1.0 target) copies; the
#: dashboard copy is held byte-identical to the packaged one by
#: ``tests/packaging_metadata/test_site_schemas_match_packaged.py``.
_HOST_MEMORY_DESCRIPTIONS = [
    ("packaged-capsule", _ROOT / "src/novafabric/schemas/run-capsule.schema.json", "Host"),
    ("oas-v1-capsule", _ROOT / "schemas/run-capsule.schema.json", "Host"),
    ("packaged-env", _ROOT / "src/novafabric/schemas/environment.schema.json", "HostSection"),
    ("oas-v1-env", _ROOT / "schemas/environment.schema.json", "HostSection"),
]


@pytest.mark.parametrize(
    ("schema_path", "host_def"),
    [(p, d) for _, p, d in _HOST_MEMORY_DESCRIPTIONS],
    ids=[i for i, _, _ in _HOST_MEMORY_DESCRIPTIONS],
)
def test_schema_documents_zero_as_unknown(schema_path: Path, host_def: str) -> None:
    """0 is the schema's only 'not measured' encoding, so a reader must be told."""
    node = json.loads(schema_path.read_text(encoding="utf-8"))["$defs"][host_def]
    field = node["properties"]["memory_bytes"]
    assert "memory_bytes" in node["required"]  # unchanged: still required
    assert field["minimum"] == 0
    assert "0 means unknown" in field["description"]
