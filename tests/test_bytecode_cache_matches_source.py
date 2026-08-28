"""A gate run against stale bytecode proves nothing — so check the caches.

Python decides whether a cached ``.pyc`` is current by comparing the source's
**mtime in whole seconds and its size**. Two different versions of one file that
share a size and are written inside the same second are therefore
indistinguishable to it, and the stale bytecode is served for as long as the
file keeps that mtime and size. Nothing warns; ``inspect.getsource`` still shows
the new code, because it reads the ``.py``.

That is not hypothetical here. On 2026-08-28 ``src/novafabric/ha/lease.py`` was
served from a ``.pyc`` holding its pre-fix ``_connect`` while the source on disk
had been fixed, and the header validated: it recorded exactly the current
source's mtime and size. Hours of measurement went into a "residual failure"
that was the old code running, and two defects were written up that do not
exist. Recompiling the whole tree costs well under a second, so the check that
Python cannot do is simply done here.

Fix when this fails::

    find src -name __pycache__ -type d -prune -exec rm -rf {} +
"""
from __future__ import annotations

import marshal
import struct
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
TAG = f"cpython-{sys.version_info.major}{sys.version_info.minor}"


def _same_code(a, b) -> bool:
    """Compare two code objects structurally, recursing into nested code."""
    if a.co_code != b.co_code or a.co_names != b.co_names:
        return False
    nested_a = [c for c in a.co_consts if hasattr(c, "co_code")]
    nested_b = [c for c in b.co_consts if hasattr(c, "co_code")]
    if len(nested_a) != len(nested_b):
        return False
    return all(_same_code(x, y) for x, y in zip(nested_a, nested_b))


def _cached_pyc(py: Path) -> Path:
    return py.parent / "__pycache__" / f"{py.stem}.{TAG}.pyc"


def test_no_cached_bytecode_disagrees_with_its_source() -> None:
    """Every ``.pyc`` under ``src/`` must match a fresh compile of its ``.py``."""
    if not SRC.is_dir():  # pragma: no cover - source checkout only
        pytest.skip("no src/ tree (installed distribution)")

    stale: list[str] = []
    compared = 0

    for py in sorted(SRC.rglob("*.py")):
        pyc = _cached_pyc(py)
        if not pyc.exists():
            continue  # nothing cached: the import will compile from source
        raw = pyc.read_bytes()
        if len(raw) <= 16:  # pragma: no cover - truncated cache
            stale.append(f"{py.relative_to(SRC)}: truncated .pyc")
            continue
        try:
            cached = marshal.loads(raw[16:])
            fresh = compile(py.read_bytes(), str(py), "exec", dont_inherit=True)
        except (SyntaxError, ValueError, EOFError) as exc:  # pragma: no cover
            stale.append(f"{py.relative_to(SRC)}: could not compare ({exc})")
            continue
        compared += 1
        if not _same_code(cached, fresh):
            flags = struct.unpack("<I", raw[4:8])[0]
            mtime, size = struct.unpack("<II", raw[8:16])
            st = py.stat()
            validated = (
                flags == 0 and mtime == int(st.st_mtime) and size == st.st_size
            )
            stale.append(
                f"{py.relative_to(SRC)}: cached bytecode differs from source "
                f"(Python's own mtime+size check says valid={validated}, so it "
                f"would keep serving this)"
            )

    if compared == 0:  # pragma: no cover - clean checkout, nothing imported yet
        pytest.skip("no cached bytecode to check")

    assert not stale, (
        "stale bytecode is being served, so any test result for these modules is "
        "meaningless:\n  "
        + "\n  ".join(stale)
        + "\n\nfix: find src -name __pycache__ -type d -prune -exec rm -rf {} +"
    )
