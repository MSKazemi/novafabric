# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Zstd dictionary-based compression registry for the Object Capsule Store (cap-005).

Architecture
------------
``ZstdDictRegistry`` maintains a name-keyed store of trained zstd dictionaries.
Callers train a dictionary once from representative samples, then pass its
``dict_id`` to ``ObjectCapsuleStore.put_capsule()`` so that repetitive payloads
(e.g. JSON capsule envelopes sharing a schema) are compressed at the block level
rather than relying on zstd's built-in LZ77 back-references alone.

Thread safety
-------------
All public methods are protected by a ``threading.RLock`` so that concurrent
``put_capsule()`` calls from different threads share one registry safely.

Lazy import
-----------
``zstandard`` is imported inside methods rather than at module level.  This
means the module can be imported without ``zstandard`` installed — the error
only surfaces when compression is actually attempted.
"""

from __future__ import annotations

import threading
import types

from novafabric.object_capsule_store.exceptions import CompressionDictNotFound


class ZstdDictRegistry:
    """Named registry of zstd training dictionaries.

    Usage::

        registry = ZstdDictRegistry()
        registry.train(samples=[capsule_bytes_1, capsule_bytes_2, ...],
                       dict_id="capsule-v1")

        store = ObjectCapsuleStore(adapter=..., ..., zstd_registry=registry)
        receipt = store.put_capsule(..., compression_dict_id="capsule-v1")
        data = store.get_capsule(...)  # automatically decompressed

    Args:
        level: Zstd compression level passed to the compressor (1–22, default 3).
               Higher values produce smaller output at the cost of CPU time.
    """

    def __init__(self, level: int = 3) -> None:
        self._level = level
        self._dicts: dict[str, bytes] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Dictionary management
    # ------------------------------------------------------------------

    def train(self, samples: list[bytes], dict_id: str) -> bytes:
        """Train a zstd dictionary from *samples* and register it as *dict_id*.

        Uses ``zstandard.ZstdCompressionDict`` training mode.  The dictionary
        bytes are stored in-process and returned to the caller for optional
        persistence.

        Args:
            samples: Representative byte strings drawn from the same data
                     distribution as future payloads.  More samples produce a
                     better dictionary; at minimum 10–20 are recommended.
            dict_id: Unique name by which this dictionary will be referenced.
                     If *dict_id* already exists, it is silently overwritten.

        Returns:
            Raw dictionary bytes (the trained zstd dict payload).

        Raises:
            ImportError: if ``zstandard`` is not installed.
            ValueError: if *samples* is empty.
        """
        if not samples:
            raise ValueError("At least one sample is required to train a zstd dictionary")
        zstd = _import_zstandard()
        dict_size = max(1024, min(112640, sum(len(s) for s in samples) // 2))
        dict_data: bytes = zstd.train_dictionary(dict_size, samples).as_bytes()
        with self._lock:
            self._dicts[dict_id] = dict_data
        return dict_data

    def register(self, dict_id: str, dict_bytes: bytes) -> None:
        """Register a pre-trained zstd dictionary (e.g. loaded from disk).

        Args:
            dict_id:    Unique name for this dictionary.
            dict_bytes: Raw zstd dictionary bytes produced by a prior ``train()``
                        call or by ``zstd --train``.
        """
        with self._lock:
            self._dicts[dict_id] = dict_bytes

    def get(self, dict_id: str) -> bytes | None:
        """Return raw dictionary bytes for *dict_id*, or ``None`` if not found."""
        with self._lock:
            return self._dicts.get(dict_id)

    # ------------------------------------------------------------------
    # Compression / decompression
    # ------------------------------------------------------------------

    def compress(self, data: bytes, dict_id: str) -> bytes:
        """Compress *data* using the dictionary registered under *dict_id*.

        Args:
            data:    Raw bytes to compress.
            dict_id: ID of a previously trained/registered dictionary.

        Returns:
            Compressed bytes.

        Raises:
            CompressionDictNotFound: if *dict_id* has not been registered.
            ImportError: if ``zstandard`` is not installed.
        """
        dict_bytes = self._require_dict(dict_id)
        zstd = _import_zstandard()
        zdict = zstd.ZstdCompressionDict(dict_bytes)
        cctx = zstd.ZstdCompressor(level=self._level, dict_data=zdict)
        result: bytes = cctx.compress(data)
        return result

    def decompress(self, data: bytes, dict_id: str) -> bytes:
        """Decompress *data* using the dictionary registered under *dict_id*.

        Args:
            data:    Compressed bytes produced by :meth:`compress`.
            dict_id: ID of the dictionary used during compression.

        Returns:
            Original (decompressed) bytes.

        Raises:
            CompressionDictNotFound: if *dict_id* has not been registered.
            ImportError: if ``zstandard`` is not installed.
        """
        dict_bytes = self._require_dict(dict_id)
        zstd = _import_zstandard()
        zdict = zstd.ZstdCompressionDict(dict_bytes)
        dctx = zstd.ZstdDecompressor(dict_data=zdict)
        result: bytes = dctx.decompress(data)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_dict(self, dict_id: str) -> bytes:
        """Return dict bytes or raise ``CompressionDictNotFound``."""
        with self._lock:
            d = self._dicts.get(dict_id)
        if d is None:
            raise CompressionDictNotFound(dict_id)
        return d


# ---------------------------------------------------------------------------
# Module-level lazy import helper
# ---------------------------------------------------------------------------

def _import_zstandard() -> types.ModuleType:
    """Import and return the ``zstandard`` module.

    Raises:
        ImportError: with an actionable message if ``zstandard`` is not installed.
    """
    try:
        import zstandard

        return zstandard
    except ImportError as exc:
        raise ImportError(
            "zstandard is required for OCS dictionary compression. "
            "Install it with: pip install 'novafabric[ocs-compress]'"
        ) from exc
