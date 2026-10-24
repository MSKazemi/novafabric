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

from novafabric._safe_path import fsync_dir

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


#: Default cap on committed segments (each holds one event, typically < 1 KiB).
DEFAULT_MAX_FILES = 10_000
#: Attempts to find a free sequence when other processes keep committing first.
_MAX_COMMIT_ATTEMPTS = 1000
#: ``checkpoint.json`` is a few dozen bytes; anything larger is not one.
_MAX_CHECKPOINT_BYTES = 64 * 1024
#: Go's "never acknowledged" sentinel (``math.MaxUint64``) and anything near it.
_NEVER_ACKED = 2**63


class SpoolFullError(OSError):
    """The spool holds ``max_files`` segments and none is acknowledged yet.

    Raised instead of evicting events no forwarder has drained; the caller counts
    the drop (review capture-runners-16).
    """


class _PurePythonSpool:
    """Pure-Python spool writer; the same on-disk format and rules as the Go writer.

    Each call to ``write()`` commits one new segment ``<seq:020d>.jsonl``: a
    16-byte header plus the event. It is staged as ``<seq>.<pid>.jsonl.tmp`` and
    committed with ``link(2)``, which fails when the name exists — ``rename(2)``
    silently replaced another process's segment. The sequence starts above every
    committed segment and the forwarder's ``checkpoint.json``, so a new writer
    never reuses an acknowledged number the reader would skip. Only segments the
    checkpoint acknowledges are ever evicted; a spool full of unforwarded events
    raises :class:`SpoolFullError` (review capture-runners-16, mirroring
    go-collector-ts-packages-03).

    Thread-safe: a ``threading.Lock`` serialises writes in this process; ``link``
    arbitrates between processes (it is a POSIX commit primitive, not a lock, and
    works on NFS and Lustre).
    """

    def __init__(self, spool_dir: Path, max_files: int = DEFAULT_MAX_FILES) -> None:
        self._dir = spool_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._seq: int = 0  # this process's floor: one above its last commit
        self._max_files = max_files
        self._pid = os.getpid()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write(self, event_json: Union[bytes, str]) -> None:
        """Write a single JSON event to a new spool segment.

        ``event_json`` must be a valid JSON object (no embedded newlines).
        Bytes or str are both accepted; str is encoded as UTF-8.

        Raises:
            SpoolFullError: ``max_files`` segments are present and none is
                acknowledged, so none may be evicted.
        """
        if isinstance(event_json, str):
            data = event_json.encode("utf-8")
        else:
            data = event_json

        with self._lock:
            committed = self._committed()
            acked = self._acked_seq()
            self._make_room(committed, acked)
            floor = max([self._seq, *(n + 1 for n, _ in committed)])
            if acked is not None:
                floor = max(floor, acked + 1)
            self._seq = self._write_segment(floor, data) + 1

    def drain(self) -> None:
        """Flush all pending events.

        The pure-Python spool is always durable after each ``write()`` (fsynced
        commit).  This method is a no-op for API compatibility with the cffi
        path.
        """

    def close(self) -> None:
        """Release resources.  No-op for the pure-Python spool."""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _committed(self) -> list[tuple[int, Path]]:
        """Committed segments as ``(seq, path)``, oldest first."""
        found: list[tuple[int, Path]] = []
        for path in self._dir.glob("*.jsonl"):
            try:
                found.append((int(path.stem), path))
            except ValueError:
                continue
        return sorted(found)

    def _acked_seq(self) -> int | None:
        """The forwarder's last acknowledged sequence, or ``None`` (none, or unreadable)."""
        try:
            with open(self._dir / "checkpoint.json", "rb") as fh:
                raw = fh.read(_MAX_CHECKPOINT_BYTES + 1)
            doc = json.loads(raw) if len(raw) <= _MAX_CHECKPOINT_BYTES else None
        except (OSError, ValueError):
            return None
        seq = doc.get("last_ack_seq") if isinstance(doc, dict) else None
        if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < _NEVER_ACKED:
            return None
        return seq

    def _make_room(self, committed: list[tuple[int, Path]], acked: int | None) -> None:
        """Evict acknowledged segments until one more fits; refuse if none can go."""
        if self._max_files <= 0:
            return
        excess = len(committed) - self._max_files + 1
        for seq, path in committed:
            if excess <= 0:
                return
            if acked is None or seq > acked:
                break  # oldest-first: everything from here on is unforwarded
            try:
                path.unlink()
            except FileNotFoundError:
                pass  # another writer evicted it
            excess -= 1
        if excess > 0:
            raise SpoolFullError(
                f"spool {self._dir} holds {len(committed)} unforwarded segment(s) "
                f"(max_files={self._max_files}); the event is not written"
            )

    def _write_segment(self, seq: int, data: bytes) -> int:
        """Commit *data* at the first free sequence from *seq*; return that sequence."""
        tmp_path = self._dir / f"{seq:020d}.{self._pid}.jsonl.tmp"
        body = data if data.endswith(b"\n") else data + b"\n"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(_encode_segment_header(self._pid, seq, write_complete=False))
                f.write(body)
                f.seek(12)  # WriteComplete = 1 only once the body is on disk
                f.write(b"\x01")
                f.flush()
                os.fsync(f.fileno())
            for _ in range(_MAX_COMMIT_ATTEMPTS):
                try:
                    os.link(tmp_path, self._dir / f"{seq:020d}.jsonl")
                except FileExistsError:
                    # Another process committed this sequence first; the header
                    # records the sequence, so rewrite it before the next try.
                    seq += 1
                    with open(tmp_path, "r+b") as f:
                        f.seek(4)
                        f.write(struct.pack("<Q", seq))
                        f.flush()
                        os.fsync(f.fileno())
                    continue
                fsync_dir(self._dir)
                return seq
            raise OSError(f"spool {self._dir}: no free sequence after {_MAX_COMMIT_ATTEMPTS} tries")
        finally:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
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
        max_files: Maximum number of committed ``.jsonl`` segments. At the cap,
            only segments the forwarder's ``checkpoint.json`` acknowledges are
            evicted; with none, :meth:`write` raises :class:`SpoolFullError`.
            Applies to the pure-Python backend only. Defaults to
            :data:`DEFAULT_MAX_FILES`.

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
        max_files: int = DEFAULT_MAX_FILES,
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
            SpoolFullError: pure-Python backend, ``max_files`` unforwarded
                segments are present (an :class:`OSError`).
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
