"""TV-5 Snapshot Store — msgpack or JSON topology snapshots."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

try:
    from prometheus_client import Histogram

    snapshot_size_histogram = Histogram(
        "novafabric_snapshot_size_bytes",
        "TV5 snapshot serialised size in bytes",
        buckets=[1024, 10240, 102400, 1048576],
    )
except ImportError:  # pragma: no cover
    snapshot_size_histogram = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# TTL constants (seconds)
_FINE_TTL_SECONDS: int = 24 * 3600  # 24 hours
_COARSE_TTL_SECONDS: int = 7 * 24 * 3600  # 7 days


class SnapshotStore3D:
    """Stores TV-5 topology snapshots.

    Fine-tier: every snapshot (up to TV5_MAX_FINE_SNAPSHOTS, default 288).
    Coarse-tier: hourly snapshots (up to TV5_MAX_COARSE_SNAPSHOTS, default 168).
    Atomic writes: write to .tmp then rename.
    """

    def __init__(self, snapshot_dir: str | Path | None = None):
        if snapshot_dir is not None:
            self.base_dir = Path(snapshot_dir)
        else:
            self.base_dir = Path(os.getenv("TV5_SNAPSHOT_DIR", ".nova/tv5_snapshots"))
        self.fine_dir = self.base_dir / "fine"
        self.coarse_dir = self.base_dir / "coarse"
        self.fine_dir.mkdir(parents=True, exist_ok=True)
        self.coarse_dir.mkdir(parents=True, exist_ok=True)
        self._max_fine = int(os.getenv("TV5_MAX_FINE_SNAPSHOTS", "288"))
        self._max_coarse = int(os.getenv("TV5_MAX_COARSE_SNAPSHOTS", "168"))

    def _encode(self, data: dict[str, Any]) -> bytes:
        try:
            import msgpack  # type: ignore[import-not-found]

            return msgpack.packb(data, use_bin_type=True)  # type: ignore[no-any-return]
        except ImportError:
            return json.dumps(data).encode()

    def _decode(self, raw: bytes) -> dict[str, Any]:
        try:
            import msgpack  # noqa: PLC0415

            return msgpack.unpackb(raw, raw=False)  # type: ignore[no-any-return]
        except ImportError:
            return json.loads(raw.decode())  # type: ignore[no-any-return]

    def _content_type(self) -> str:
        try:
            import msgpack  # noqa: PLC0415,F401

            return "application/msgpack"
        except ImportError:
            return "application/json"

    def save(self, window_id: str, snapshot: dict[str, Any]) -> None:
        """Save a fine-tier snapshot. Atomic write."""
        path = self.fine_dir / f"{window_id}.snap"
        tmp = path.with_suffix(".tmp")
        encoded = self._encode(snapshot)
        if snapshot_size_histogram is not None:
            snapshot_size_histogram.observe(len(encoded))
        tmp.write_bytes(encoded)
        tmp.rename(path)
        self._enforce_fine_retention()

    def get(self, window_id: str) -> dict[str, Any] | None:
        """Return snapshot or None if not found/expired."""
        for d in [self.fine_dir, self.coarse_dir]:
            path = d / f"{window_id}.snap"
            if path.exists():
                try:
                    return self._decode(path.read_bytes())
                except Exception:  # noqa: BLE001
                    return None
        return None

    def list_windows(self) -> list[dict[str, Any]]:
        """List all available snapshot window IDs sorted by modification time."""
        windows: list[dict[str, Any]] = []
        for d in [self.fine_dir, self.coarse_dir]:
            for p in sorted(d.glob("*.snap"), key=lambda x: x.stat().st_mtime, reverse=True):
                windows.append(
                    {
                        "windowId": p.stem,
                        "timestamp": int(p.stat().st_mtime),
                        "tier": d.name,
                    }
                )
        return windows

    def _enforce_fine_retention(self) -> None:
        snaps = sorted(
            self.fine_dir.glob("*.snap"), key=lambda x: x.stat().st_mtime, reverse=True
        )
        for old in snaps[self._max_fine :]:
            try:
                old.unlink()
            except Exception:  # noqa: BLE001
                pass

    def evict_expired(self) -> tuple[int, int]:
        """Remove snapshots older than the TTL for each tier.

        Returns:
            (fine_evicted, coarse_evicted) — counts of files removed.
        """
        now = time.time()
        fine_evicted = 0
        coarse_evicted = 0

        for snap in list(self.fine_dir.glob("*.snap")):
            try:
                if now - snap.stat().st_mtime > _FINE_TTL_SECONDS:
                    snap.unlink()
                    fine_evicted += 1
            except Exception:  # noqa: BLE001
                pass

        for snap in list(self.coarse_dir.glob("*.snap")):
            try:
                if now - snap.stat().st_mtime > _COARSE_TTL_SECONDS:
                    snap.unlink()
                    coarse_evicted += 1
            except Exception:  # noqa: BLE001
                pass

        if fine_evicted or coarse_evicted:
            logger.info(
                "TV-5 TTL eviction: fine=%d coarse=%d", fine_evicted, coarse_evicted
            )
        return fine_evicted, coarse_evicted

    async def start_retention_loop(self, interval_seconds: float = 300.0) -> None:
        """Asyncio task: run evict_expired every *interval_seconds* (default 5 min).

        Call this once from the lifespan context:
            asyncio.create_task(store.start_retention_loop())
        """
        while True:
            try:
                self.evict_expired()
            except Exception as exc:  # noqa: BLE001
                logger.warning("TV-5 retention loop error: %s", exc)
            await asyncio.sleep(interval_seconds)

    @property
    def content_type(self) -> str:
        return self._content_type()
