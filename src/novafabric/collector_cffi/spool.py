"""cffi-based Python wrapper for the NovaFabric Go spool library.

The Go shared library (libnovaspool.so) is built via::

    cd collector && go build -buildmode=c-shared -o ../lib/libnovaspool.so ./pkg/cffi/

If the shared library is not present, falls back to a pure-Python atomic-rename
spool implementation that matches the Go spool wire format (16-byte binary header
+ JSONL event body, committed via os.rename from a .tmp staging file).

Usage::

    from novafabric.collector_cffi.spool import NovaPySpool

    spool = NovaPySpool("/tmp/my-spool")
    spool.write(b'{"event_type": "run.start", ...}')
    spool.drain()
    spool.close()
"""

from __future__ import annotations

import json
import os
import struct
import threading
import time
from pathlib import Path
from typing import Any, Union

# ---------------------------------------------------------------------------
# Constants matching collector/internal/spool/segment.go
# ---------------------------------------------------------------------------

_HEADER_SIZE: int = 16
_HEADER_VERSION: int = 1

# ---------------------------------------------------------------------------
# cffi load helpers
# ---------------------------------------------------------------------------


def _find_libnovaspool() -> str | None:
    """Return the path to libnovaspool.so, or None if not found.

    Search order:
    1. ``NOVA_LIBSPOOL_PATH`` environment variable (absolute path to .so).
    2. ``<repo-root>/lib/libnovaspool.so`` (relative to this file's location).
    3. Standard LD_LIBRARY_PATH / dynamic linker paths via ``ctypes.util.find_library``.
    """
    env_path = os.environ.get("NOVA_LIBSPOOL_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    # Resolve relative to src/novafabric/collector_cffi/spool.py → ../../../../lib/
    here = Path(__file__).resolve().parent
    candidate = here.parents[3] / "lib" / "libnovaspool.so"
    if candidate.is_file():
        return str(candidate)

    # Fallback: ask the dynamic linker.
    try:
        import ctypes.util

        name = ctypes.util.find_library("novaspool")
        if name:
            return name
    except Exception:  # pragma: no cover
        pass

    return None


def _load_cffi_lib(lib_path: str) -> tuple[Any, Any]:
    """Load libnovaspool.so via cffi and return (ffi, lib) tuple."""
    import cffi

    ffi: Any = cffi.FFI()
    ffi.cdef(
        """
        int SpoolWrite(const char *spool_dir, const char *event_json, int event_len);
        int SpoolDrain(const char *spool_dir);
        char *SpoolLastError(void);
        void free(void *ptr);
        """
    )
    lib: Any = ffi.dlopen(lib_path)
    return ffi, lib


# ---------------------------------------------------------------------------
# Pure-Python fallback implementation
# ---------------------------------------------------------------------------


def _encode_segment_header(pid: int, seq: int, write_complete: bool) -> bytes:
    """Encode the 16-byte segment header (little-endian).

    Wire layout (matches collector/internal/spool/segment.go):
        Bytes  0– 3   Writer PID      (uint32, LE)
        Bytes  4–11   Sequence        (uint64, LE)
        Byte  12      WriteComplete   (0 or 1)
        Byte  13      Version         (always 1)
        Bytes 14–15   Reserved        (0x00 0x00)
    """
    return struct.pack(
        "<IQBBxx",
        pid & 0xFFFFFFFF,
        seq & 0xFFFFFFFFFFFFFFFF,
        1 if write_complete else 0,
        _HEADER_VERSION,
    )


class _PurePythonSpool:
    """Pure-Python atomic-rename spool writer.

    Each call to ``write()`` produces a new segment file:
        <spool_dir>/<20-digit-zero-padded-seq>.jsonl

    Files are staged as ``.jsonl.tmp`` and atomically renamed to ``.jsonl``
    on commit — the same invariant as the Go spool.

    Thread-safe: a ``threading.Lock`` serialises the sequence counter and
    file rename so that concurrent writers never collide on the same sequence
    number.
    """

    def __init__(self, spool_dir: Path, max_files: int = 100) -> None:
        self._dir = spool_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq: int = 0
        self._max_files = max_files
        self._pid = os.getpid()

        # Scan existing .jsonl files to set the starting sequence number so
        # that new files never overwrite committed segments from a prior run.
        existing = sorted(self._dir.glob("*.jsonl"))
        if existing:
            last_name = existing[-1].stem  # e.g. "00000000000000000042"
            try:
                self._seq = int(last_name) + 1
            except ValueError:
                self._seq = len(existing)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, event_json: Union[bytes, str]) -> None:
        """Write a single JSON event to a new spool segment.

        ``event_json`` must be a valid JSON object (no embedded newlines).
        Bytes or str are both accepted; str is encoded as UTF-8.
        """
        if isinstance(event_json, str):
            data = event_json.encode("utf-8")
        else:
            data = event_json

        with self._lock:
            self._evict_if_needed()
            seq = self._seq
            self._seq += 1

        self._write_segment(seq, data)

    def drain(self) -> None:
        """Flush all pending events.

        The pure-Python spool is always durable after each ``write()`` (atomic
        rename).  This method is a no-op for API compatibility with the cffi
        path.
        """

    def close(self) -> None:
        """Release resources.  No-op for the pure-Python spool."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_segment(self, seq: int, data: bytes) -> None:
        """Write one segment file for *seq* containing *data*."""
        tmp_name = f"{seq:020d}.jsonl.tmp"
        dst_name = f"{seq:020d}.jsonl"
        tmp_path = self._dir / tmp_name
        dst_path = self._dir / dst_name

        # Stage to .tmp — write incomplete header first.
        incomplete_hdr = _encode_segment_header(self._pid, seq, write_complete=False)
        body = data if data.endswith(b"\n") else data + b"\n"

        with open(tmp_path, "wb") as f:
            f.write(incomplete_hdr)
            f.write(body)
            # Seek back to byte 12 and set WriteComplete = 1.
            f.seek(12)
            f.write(b"\x01")
            f.flush()
            os.fsync(f.fileno())

        # Atomic rename — the ONLY commit primitive.
        os.rename(str(tmp_path), str(dst_path))

    def _evict_if_needed(self) -> None:
        """Remove the oldest .jsonl files when the count exceeds *max_files*.

        Must be called with ``self._lock`` held.
        """
        if self._max_files <= 0:
            return
        committed = sorted(self._dir.glob("*.jsonl"))
        overflow = len(committed) - self._max_files
        if overflow > 0:
            for path in committed[:overflow]:
                try:
                    path.unlink()
                except OSError:  # pragma: no cover
                    pass


# ---------------------------------------------------------------------------
# cffi-backed spool implementation
# ---------------------------------------------------------------------------


class _CFFISpool:
    """cffi-backed spool writer using libnovaspool.so."""

    def __init__(self, spool_dir: Path, ffi: Any, lib: Any) -> None:
        self._dir = str(spool_dir)
        spool_dir.mkdir(parents=True, exist_ok=True)
        self._ffi = ffi
        self._lib = lib

    def write(self, event_json: Union[bytes, str]) -> None:
        """Write a single JSON event via the Go SpoolWrite C export."""
        if isinstance(event_json, str):
            data = event_json.encode("utf-8")
        else:
            data = event_json

        rc = self._lib.SpoolWrite(
            self._ffi.new("char[]", self._dir.encode()),
            self._ffi.new("char[]", data),
            len(data),
        )
        if rc != 0:
            err_ptr = self._lib.SpoolLastError()
            try:
                err_msg = self._ffi.string(err_ptr).decode("utf-8", errors="replace")
            finally:
                self._lib.free(err_ptr)
            raise OSError(f"SpoolWrite failed: {err_msg}")

    def drain(self) -> None:
        """Call SpoolDrain to signal a flush."""
        rc = self._lib.SpoolDrain(self._ffi.new("char[]", self._dir.encode()))
        if rc != 0:
            err_ptr = self._lib.SpoolLastError()
            try:
                err_msg = self._ffi.string(err_ptr).decode("utf-8", errors="replace")
            finally:
                self._lib.free(err_ptr)
            raise OSError(f"SpoolDrain failed: {err_msg}")

    def close(self) -> None:
        """Release resources (no-op for the cffi backend)."""


# ---------------------------------------------------------------------------
# Public facade
# ---------------------------------------------------------------------------


class NovaPySpool:
    """Python spool writer for NovaFabric events.

    Tries to load ``libnovaspool.so`` at construction time.  Falls back to a
    pure-Python implementation that produces the same on-disk format if the
    shared library is not available.

    Args:
        spool_dir: Directory where spool segment files are written.  Created
            automatically if it does not exist.
        max_files: Maximum number of committed ``.jsonl`` files to retain in
            the spool directory before evicting the oldest ones.  Applies to
            the pure-Python backend only.  Defaults to 100.

    Example::

        spool = NovaPySpool("/var/spool/novafabric")
        spool.write(b'{"event_type": "run.start", ...}')
        spool.drain()
        spool.close()
    """

    def __init__(
        self,
        spool_dir: Union[str, Path],
        *,
        max_files: int = 100,
    ) -> None:
        self._spool_dir = Path(spool_dir)
        self._backend: _PurePythonSpool | _CFFISpool

        lib_path = _find_libnovaspool()
        if lib_path is not None:
            try:
                ffi, lib = _load_cffi_lib(lib_path)
                self._backend = _CFFISpool(self._spool_dir, ffi, lib)
                self._using_cffi = True
                return
            except Exception:  # pragma: no cover
                # cffi load failed — fall through to pure-Python backend.
                pass

        self._backend = _PurePythonSpool(self._spool_dir, max_files=max_files)
        self._using_cffi = False

    @property
    def using_cffi(self) -> bool:
        """True when the cffi-backed Go library is active."""
        return self._using_cffi

    def write(self, event_json: Union[bytes, str]) -> None:
        """Write a single JSON event to the spool.

        ``event_json`` must be a JSON object without embedded newlines.
        Bytes or str are accepted.

        Raises:
            OSError: If the cffi backend returns an error.
            ValueError: If ``event_json`` is empty.
        """
        if not event_json:
            raise ValueError("event_json must be non-empty")
        self._backend.write(event_json)

    def drain(self) -> None:
        """Signal the spool to flush all pending events.

        For the pure-Python backend this is a no-op (every write is already
        durable via atomic rename).  For the cffi backend this calls
        ``SpoolDrain``.
        """
        self._backend.drain()

    def close(self) -> None:
        """Release resources held by the spool.

        After ``close()`` the spool must not be used.
        """
        self._backend.close()

    def __enter__(self) -> "NovaPySpool":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Minimal event envelope builder (used by integration tests)
# ---------------------------------------------------------------------------


def build_minimal_envelope(
    event_type: str = "run.start",
    *,
    run_id: str | None = None,
    agent_id: str = "test-agent",
) -> bytes:
    """Return a minimal EventEnvelope v1 JSON blob suitable for spool.write().

    The returned bytes pass ``jsonschema`` validation against
    ``schemas/event-envelope-v1/envelope-v1.json``.

    This helper is intended for tests only and is not part of the public API.
    """
    import uuid

    def _ulid() -> str:
        """Return a syntactically valid 26-char Crockford Base32 ULID stub."""
        import time as _t

        ts_ms = int(_t.time() * 1000)
        # Encode 10-byte (80-bit) value as 26 Crockford Base32 chars.
        alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
        rand_hi = int.from_bytes(os.urandom(8), "big") & 0xFFFFFFFFFFFF
        value = (ts_ms << 48) | rand_hi
        chars: list[str] = []
        for _ in range(26):
            chars.append(alphabet[value & 0x1F])
            value >>= 5
        return "".join(reversed(chars))

    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000+00:00", time.gmtime())
    rid = run_id or _ulid()
    trace_id = uuid.uuid4().hex + uuid.uuid4().hex[:0]
    trace_id = trace_id[:32]
    span_id = uuid.uuid4().hex[:16]

    envelope: dict[str, object] = {
        "envelope_version": "1",
        "schema_version": "event-envelope/1",
        "event_id": _ulid(),
        "global_run_id": rid,
        "run_id": rid,
        "parent_run_id": None,
        "event_type": event_type,
        "trace_id": trace_id,
        "span_id": span_id,
        "agent_id": agent_id,
        "cluster_id": None,
        "tenant_id": None,
        "started_at": ts,
        "payload": None,
        "payload_hash": None,
    }
    return json.dumps(envelope, separators=(",", ":")).encode("utf-8")
