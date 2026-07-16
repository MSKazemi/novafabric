"""FastAPI app factory for `nova serve --experimental`.

Layer A (read-only) — see ADR-0027 for governance and Layer B/C scope.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.metadata as importlib_metadata
import importlib.util
import json
import logging
import os
import stat
import threading
import time
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from novafabric.registry.runs_cache import (
    count_cached_runs,
)
from novafabric.registry.runs_cache import (
    query_runs as _query_runs_cache,
)
from novafabric.serve import audit
from novafabric.serve import reports as _reports
from novafabric.serve.auth import is_localhost_host
from novafabric.serve.capsule_loader import (
    discover_capsule_dirs,
    discover_ingestable_dirs,
    list_run_summaries,
    load_capsule_manifest,
    load_full_capsule,
    load_jsonl,
)

logger = logging.getLogger(__name__)


# ---------- Cursor pagination helpers (B-1) ----------

def _encode_cursor(ts: str, run_id: str) -> str:
    """Encode (timestamp, run_id) into an opaque URL-safe base64 string."""
    payload = json.dumps({"ts": ts, "id": run_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    """Decode a cursor string. Returns (ts, run_id) or None if invalid/absent."""
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor.encode() + b"==")
        obj = json.loads(raw)
        return (obj["ts"], obj["id"])
    except Exception:  # noqa: BLE001
        return None


# ---------- SSE event bus (B-3) ----------

class _RunEventBus:
    """Thread-safe in-memory broadcast bus for new-run SSE events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queues: list[asyncio.Queue[dict[str, Any]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        with self._lock:
            self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            try:
                self._queues.remove(q)
            except ValueError:
                pass

    def publish(self, run: dict[str, Any]) -> None:
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            try:
                q.put_nowait(run)
            except asyncio.QueueFull:
                pass  # slow consumer — drop rather than block


_run_bus = _RunEventBus()


# ---------- Stats cache (B-4) ----------

class _StatsCache:
    """Holds a cached stats snapshot refreshed every 30 seconds."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] | None = None
        self._cached_at: float | None = None
        self.TTL = 30.0  # seconds

    def get(self) -> dict[str, Any] | None:
        with self._lock:
            if self._data is None:
                return None
            if time.monotonic() - (self._cached_at or 0) > self.TTL:
                return None
            return dict(self._data)

    def set(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = dict(data)
            self._cached_at = time.monotonic()

    def get_or_compute(
        self, compute: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """Return the cached snapshot, computing it under the lock on a miss.

        Uses double-checked locking so that when several requests hit a cold
        (or expired) cache at once, only the first acquires the lock and runs
        ``compute``; the rest re-check inside the lock and reuse the freshly
        stored snapshot instead of all recomputing.
        """
        cached = self.get()
        if cached is not None:
            return cached
        with self._lock:
            # Re-check inside the lock — another thread may have filled it.
            if self._data is not None and (
                time.monotonic() - (self._cached_at or 0) <= self.TTL
            ):
                return dict(self._data)
            fresh = compute()
            self._data = dict(fresh)
            self._cached_at = time.monotonic()
            return dict(fresh)

    def cached_at_iso(self) -> str | None:
        with self._lock:
            if self._cached_at is None:
                return None
            import datetime
            dt = datetime.datetime.fromtimestamp(
                time.time() - (time.monotonic() - self._cached_at),
                tz=datetime.timezone.utc,
            )
            return dt.isoformat()


_stats_cache = _StatsCache()


def _get_version() -> str:
    try:
        return importlib_metadata.version("novafabric")
    except importlib_metadata.PackageNotFoundError:
        return "unknown"


# ---------- Layer B request bodies (module-level so Pydantic can resolve them) ----------


class RegisterAssetRequest(BaseModel):
    spec_yaml: str
    confirmed: bool = False


class EvalRequest(BaseModel):
    confirmed: bool = False


class ExportEvidenceRequest(BaseModel):
    output_path: str | None = None
    key_path: str | None = None
    allow_unsafe_skips: bool = False
    confirmed: bool = False


class PromoteRequest(BaseModel):
    to_status: str  # "staging" | "production" | "archived"
    actor: str = "dashboard"
    force: bool = False
    confirmed: bool = False


class ForensicReplayRequest(BaseModel):
    confirmed: bool = False


class DryRunReplayRequest(BaseModel):
    confirmed: bool = False


class SemanticReplayRequest(BaseModel):
    confirmed: bool = False


class ExactReplayRequest(BaseModel):
    confirmed: bool = False


class RedactRequest(BaseModel):
    confirmed: bool = False


class PlaceHoldRequest(BaseModel):
    registry: str
    reason: str
    duration_days: int | None = None


class RollbackRequest(BaseModel):
    reason: str = ""
    actor: str = "dashboard"
    confirmed: bool = False


class ApproveRequest(BaseModel):
    role: str
    actor: str = "dashboard"
    note: str = ""
    confirmed: bool = False


class IssueTokenRequest(BaseModel):
    label: str = "dashboard-issued"
    confirmed: bool = False


class AssignRoleRequest(BaseModel):
    subject: str
    role: str


class MCPScanRequest(BaseModel):
    manifest: dict[str, Any]
    threshold: str = "HIGH"


def _extract_score(raw: object) -> float | None:
    """Extract a float score from a raw score_json column value.

    Rules (in order):
    1. None / SQL NULL → None
    2. Parse as JSON; if it fails → None
    3. Bare int/float → use directly
    4. Dict with numeric "score" key → use that value
    5. Otherwise → None
    """
    import json as _json

    if raw is None:
        return None
    try:
        parsed = _json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, (int, float)):
        return float(parsed)
    if isinstance(parsed, dict) and isinstance(parsed.get("score"), (int, float)):
        return float(parsed["score"])
    return None


def _list_evidence_bundles() -> list[dict[str, Any]]:
    """Scan evidence dir and return EvidenceSummary dicts, newest-first."""
    import hashlib
    import json
    import zipfile

    override = os.environ.get("NOVAFABRIC_EVIDENCE_DIR")
    evidence_dir = Path(override) if override else Path.home() / ".novafabric" / "evidence"
    if not evidence_dir.is_dir():
        return []

    results = []
    for zip_path in sorted(
        evidence_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        bundle_id = zip_path.stem  # run_id = filename without .zip
        size_bytes = zip_path.stat().st_size
        verified = False
        run_id = bundle_id
        timestamp: str | None = None

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                if "manifest.json" in zf.namelist():
                    raw = zf.read("manifest.json")
                    manifest = json.loads(raw)
                    run_id = manifest.get("run_id", bundle_id)
                    timestamp = manifest.get("created_at") or manifest.get("timestamp")
                    # Shallow integrity check: recompute manifest_hash
                    stored_hash = manifest.get("manifest_hash", "")
                    work = {k: v for k, v in manifest.items() if k != "manifest_hash"}
                    recomputed = hashlib.sha256(
                        json.dumps(work, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    verified = stored_hash == recomputed
        except Exception:  # noqa: BLE001
            pass  # leave verified=False, use defaults

        results.append({
            "bundle_id": bundle_id,
            "run_id": run_id,
            "timestamp": timestamp or "",
            "size_bytes": size_bytes,
            "verified": verified,
        })

    return results


def create_app(
    *,
    token: str,
    capsule_dir: Path,
    db_path: Path | None = None,
    static_dir: Path | None = None,
    topology_enabled: bool = False,
    topology_louvain_resolution: float | None = None,
) -> FastAPI:
    """Build the FastAPI app. Pure factory; no side effects beyond app creation."""

    _db_path = db_path  # capture for lifespan closure

    # Mutable holder so that topology startup hooks (defined later in the
    # topology_enabled block) are visible to _lifespan at runtime.  The
    # topology block populates these before the event loop ever calls _lifespan.
    _topo_hooks: dict[str, Any] = {
        "seed_fn": None,   # async () -> dict — set when topology_enabled
        "loop_fn": None,   # async () -> None — set when topology_enabled
    }

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: ARG001
        try:
            from novafabric.registry.service import list_assets
            count = len(list_assets(None, None, db_path=_db_path))
            if count == 0:
                default_db = Path.home() / ".novafabric" / "registry.db"
                logger.warning(
                    "Registry is empty — run `nova register <spec.yaml>` to add assets. "
                    "DB path: %s",
                    _db_path or default_db,
                )
            else:
                logger.info("Registry loaded: %d asset(s) available.", count)
        except Exception:  # noqa: BLE001
            pass  # never block startup on a registry check failure

        # ClickHouse: auto-apply schema migrations when URL is configured.
        if os.environ.get("NOVA_CLICKHOUSE_URL"):
            try:
                from novafabric.cost.clickhouse_store import ensure_schema
                await asyncio.get_event_loop().run_in_executor(None, ensure_schema)
            except Exception as _exc:  # noqa: BLE001
                logger.warning("clickhouse: schema migration failed (non-fatal): %s", _exc)

        # Scale-S3: delegate startup indexing to CapsuleWatcher.
        try:
            _indexed = _watcher.ingest_all()
            if _indexed:
                logger.info(
                    "runs index: indexed %d capsule(s) on startup [%s backend].",
                    _indexed, _watcher.backend_name(),
                )
        except Exception as _exc:  # noqa: BLE001
            logger.debug("runs index: startup build failed (non-fatal): %s", _exc)

        # Auto-seed topology from existing capsules on disk (if enabled).
        if _topo_hooks["seed_fn"] is not None:
            try:
                _tv5_pipe_at_startup = getattr(app.state, "tv5_layout_pipe", None)
                _seed_result = await _topo_hooks["seed_fn"](tv5_pipe=_tv5_pipe_at_startup)
                logger.info(
                    "topology: auto-seeded %d agent(s), %d edge(s) on startup",
                    _seed_result.get("agents_added", 0),
                    _seed_result.get("edges_added", 0),
                )
            except Exception as _exc:  # noqa: BLE001
                logger.warning("topology: startup seed failed: %s", _exc)

        # Start KG auto-ingest background task.  The loop itself is a no-op
        # until the KG store is initialised, so it is always safe to launch.
        _kg_task = asyncio.create_task(
            _kg_auto_ingest_loop(), name="nova-serve-kg-auto-ingest"
        )
        # Start ClickHouse cost auto-ingest background task.  No-op when
        # NOVA_CLICKHOUSE_URL is not set.
        _cost_task = asyncio.create_task(
            _cost_auto_ingest_loop(), name="nova-serve-cost-auto-ingest"
        )
        # Start topology periodic re-seed loop (no-op when topology is disabled).
        _topo_task: asyncio.Task | None = None  # type: ignore[type-arg]
        if _topo_hooks["loop_fn"] is not None:
            _topo_task = asyncio.create_task(
                _topo_hooks["loop_fn"](), name="nova-serve-topology-auto-reseed"
            )

        # Stats-refresh / SSE-publish / incremental-index daemon thread. Started
        # here (not at app construction) and joined in the finally below, so its
        # lifetime is bounded by the app lifespan — see _stats_refresh_loop.
        _stats_stop = threading.Event()
        _refresh_thread = threading.Thread(
            target=_stats_refresh_loop, args=(_stats_stop,),
            daemon=True, name="nova-serve-stats-refresh",
        )
        _refresh_thread.start()
        try:
            yield
        finally:
            # Stop the stats thread first so it is not mid-poll on the watcher.
            _stats_stop.set()
            _refresh_thread.join(timeout=5.0)
            try:
                _watcher.close()  # stop the CapsuleWatcher / watchdog Observer
            except Exception:  # noqa: BLE001
                pass
            # Release the TV-5 layout ProcessPoolExecutor if topology was enabled.
            _tv5_pipe = getattr(app.state, "tv5_layout_pipe", None)
            if _tv5_pipe is not None:
                _close = getattr(_tv5_pipe, "close", None)
                if callable(_close):
                    try:
                        _close()
                    except Exception:  # noqa: BLE001
                        pass
            _bg_tasks = [_kg_task, _cost_task]
            if _topo_task is not None:
                _bg_tasks.append(_topo_task)
            for _t in _bg_tasks:
                _t.cancel()
                try:
                    await _t
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="NovaFabric — local dashboard (experimental)",
        version=_get_version(),
        description=(
            "Read-only HTTP API over the local registry, lineage, and capsule "
            "files. Per ADR-0027. Bind 127.0.0.1 only; token required on /api/*."
        ),
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    # CORS: only same-origin and localhost dev servers (Astro at :4321 by default)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:4321", "http://127.0.0.1:4321",
            "http://localhost:4322", "http://127.0.0.1:4322",
        ],
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["*"],
        allow_credentials=False,
    )

    # ---------- middleware ----------

    @app.middleware("http")
    async def host_header_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        # DNS-rebinding defence: only allow localhost hosts.
        if not is_localhost_host(request.headers.get("host")):
            return JSONResponse(
                status_code=403,
                content={"error": "host_not_localhost", "host": request.headers.get("host")},
            )
        return await call_next(request)

    # ---------- auth dependency ----------

    def verify_token(t: str | None = Query(default=None, alias="token")) -> str:
        if not t or not _consteq(t, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="missing or invalid token",
            )
        # Returns a short fingerprint for audit logging.
        return t[:8]

    # ---------- routes ----------

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        # Health is unauthenticated by design — useful for browser open-redirect
        # check. Returns nothing sensitive.
        import shutil

        # Extended system health — each check is wrapped so one failure never breaks the response.
        try:
            backend_type = "postgres" if os.environ.get("NOVAFABRIC_DB_URL") else "sqlite"
        except Exception:  # noqa: BLE001
            backend_type = "sqlite"

        try:
            resolved_db_path = str(db_path or Path.home() / ".novafabric" / "registry.db")
        except Exception:  # noqa: BLE001
            resolved_db_path = ""

        try:
            keystore_ok = (Path.home() / ".novafabric" / "keys").exists()
        except Exception:  # noqa: BLE001
            keystore_ok = False

        try:
            opa_available = shutil.which("opa") is not None
        except Exception:  # noqa: BLE001
            opa_available = False

        try:
            from novafabric.trust.novaseal.config import load_signing_profile  # noqa: PLC0415
            novaseal_configured = load_signing_profile() is not None
        except Exception:  # noqa: BLE001
            novaseal_configured = False

        return {
            "ok": True,
            "service": "nova-serve",
            "version": _get_version(),
            "experimental": True,
            "docs": "/api/docs",
            "backend_type": backend_type,
            "db_path": resolved_db_path,
            "keystore_ok": keystore_ok,
            "opa_available": opa_available,
            "novaseal_configured": novaseal_configured,
            "schema_version": "0.1.0",
        }

    def _compute_stats() -> dict[str, Any]:
        """Compute fresh aggregate stats. May be called from background thread."""
        from novafabric.registry.runs_cache import count_cached_runs  # noqa: PLC0415
        from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415

        conn = get_connection(db_path)
        init_schema(conn)
        try:
            cached = count_cached_runs(conn)
            if cached > 0:
                # Fast path: query the index (O(1) SQL).
                run_count = conn.execute("SELECT COUNT(*) FROM runs_cache").fetchone()[0]
                failed_count = conn.execute(
                    "SELECT COUNT(*) FROM runs_cache WHERE status != 'success'"
                ).fetchone()[0]
            else:
                # Fallback: disk scan (first startup before index is built).
                summaries = list_run_summaries(capsule_dir)
                run_count = len(summaries)
                failed_count = sum(1 for s in summaries if s.get("status") != "success")

            asset_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM assets WHERE status = 'staging'"
            ).fetchone()[0]
            production_count = conn.execute(
                "SELECT COUNT(*) FROM assets WHERE status = 'production'"
            ).fetchone()[0]
        finally:
            conn.close()

        return {
            "run_count": run_count,
            "failed_run_count": failed_count,
            "passed_run_count": run_count - failed_count,
            "asset_count": asset_count,
            "pending_eval_count": pending_count,
            "production_asset_count": production_count,
        }

    # Scale-S3: one CapsuleWatcher instance for the full app lifetime.
    from novafabric.serve.capsule_watcher import CapsuleWatcher as _CapsuleWatcher  # noqa: PLC0415
    _watcher = _CapsuleWatcher(capsule_dir, db_path=_db_path)

    def _stats_refresh_loop(stop: threading.Event) -> None:  # runs in a daemon thread
        """Refresh stats cache every 30 s and publish new runs to the SSE bus.

        Also performs incremental runs-index updates so the /api/runs index
        stays current without full O(N) disk scans on every API call.

        Exits promptly when *stop* is set. The thread is started and stopped by
        the app lifespan (see ``_lifespan``) — it does **not** start at app
        construction time, so a ``TestClient`` used without its context manager
        (no lifespan) never spawns it, which keeps the test suite from
        accumulating one un-joined daemon thread per ``create_app()``.
        """
        prev_run_ids: set[str] = set()
        first_run = True
        while not stop.is_set():
            try:
                data = _compute_stats()
                _stats_cache.set(data)
                # Scale-S3: incremental index update via CapsuleWatcher.
                _watcher.poll_once()
                # Query the index to find new run IDs for SSE broadcast.
                from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415
                _conn = get_connection(db_path)
                init_schema(_conn)
                _all_rows, _ = _query_runs_cache(_conn, limit=10_000)
                _conn.close()
                current_ids: set[str] = {r["run_id"] for r in _all_rows if r.get("run_id")}
                if not first_run:
                    new_ids = current_ids - prev_run_ids
                    for r in _all_rows:
                        if r.get("run_id") in new_ids:
                            _run_bus.publish(r)
                prev_run_ids = current_ids
                first_run = False
            except Exception:  # noqa: BLE001
                pass
            if stop.wait(2.0):  # poll every 2 s for SSE freshness; wake early on stop
                break

    @app.get("/api/stats", dependencies=[Depends(verify_token)])
    async def get_stats() -> dict[str, Any]:
        """Aggregate counts for the HomeTab.

        Returns cached counts (approximate: true) when the 30-second cache
        is warm, or computes fresh counts on the first request.
        """
        # Double-checked locking: on a cold/expired cache, concurrent
        # cache-miss requests do not all recompute — only the first does.
        data = _stats_cache.get_or_compute(_compute_stats)
        return {
            **data,
            "approximate": True,
            "cached_at": _stats_cache.cached_at_iso(),
        }

    @app.get("/api/runs", dependencies=[Depends(verify_token)])
    async def list_runs(
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
        since: str | None = Query(default=None, description="ISO-8601 lower bound on created_at"),
        until: str | None = Query(default=None, description="ISO-8601 upper bound on created_at"),
        status: str | None = Query(default=None, description="Filter by run status"),
        q: str | None = Query(default=None, description="Free-text search on run_id and command"),
    ) -> dict[str, Any]:
        from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415
        _conn = get_connection(db_path)
        init_schema(_conn)
        try:
            if count_cached_runs(_conn) > 0:
                page, total = _query_runs_cache(
                    _conn, limit=limit, offset=offset,
                    since=since, until=until, status=status, q=q,
                )
            else:
                summaries = list_run_summaries(capsule_dir)
                if since:
                    summaries = [s for s in summaries if (s.get("created_at") or "") >= since]
                if until:
                    summaries = [s for s in summaries if (s.get("created_at") or "") <= until]
                if status and status != "all":
                    summaries = [s for s in summaries if s.get("status") == status]
                if q:
                    q_lower = q.lower()
                    summaries = [
                        s for s in summaries
                        if q_lower in " ".join(s.get("command") or []).lower()
                        or q_lower in (s.get("run_id") or "").lower()
                    ]
                total = len(summaries)
                page = summaries[offset : offset + limit]
        finally:
            _conn.close()
        return {
            "capsule_dir": str(capsule_dir.resolve()),
            "count": total,
            "total": total,
            "has_more": offset + limit < total,
            "limit": limit,
            "offset": offset,
            "runs": page,
        }

    # Declared before /api/runs/{run_id} so FastAPI does not swallow it as a wildcard match.
    @app.get("/api/runs/suggest-register", dependencies=[Depends(verify_token)])
    async def suggest_register_endpoint(
        capsule_limit: int = Query(default=10, ge=1, le=50),
    ) -> dict[str, Any]:
        """Return asset registration suggestions derived from recent capsules.

        Skips assets already present in the registry.
        """
        try:
            from novafabric.registry.suggestion_engine import SuggestionEngine
        except ImportError:
            return {"total_capsules_analyzed": 0, "suggestions": []}

        engine = SuggestionEngine()
        try:
            suggestions = engine.analyze_recent(
                capsule_dir, db_path=db_path, limit=capsule_limit
            )
        except Exception:  # noqa: BLE001 — never 500 on a hints endpoint
            suggestions = []
        return {
            "total_capsules_analyzed": capsule_limit,
            "suggestions": [
                {
                    "asset_type": s.asset_type,
                    "detected_name": s.detected_name,
                    "detected_version": getattr(s, "detected_version", None),
                    "confidence": s.confidence,
                    "call_count": s.call_count,
                    "draft_spec_yaml": s.draft_spec_yaml,
                    "warnings": s.warnings,
                }
                for s in suggestions
            ],
        }

    # ---------- per-run cost summary (DB-COST-2) ----------

    @app.get("/api/runs/cost-summary", dependencies=[Depends(verify_token)])
    async def runs_cost_summary(
        run_ids: str = Query(description="Comma-separated list of run IDs (max 100)"),
    ) -> dict[str, Any]:
        """Return per-run token and cost totals from ClickHouse.

        Response shape::

            {"costs": {"<run_id>": {"input_tokens": int, "output_tokens": int,
                                    "cost_usd": float, "calls": int}, ...}}

        When ``NOVA_CLICKHOUSE_URL`` is not set the response is
        ``{"costs": {}}`` (200 OK, no error).  When ClickHouse is
        unreachable the response is ``{"costs": {}, "error": "<msg>"}``
        (also 200 OK — the UI degrades gracefully).
        """
        clickhouse_url = os.environ.get("NOVA_CLICKHOUSE_URL")
        if not clickhouse_url:
            return {"costs": {}}

        ids = [rid.strip() for rid in run_ids.split(",") if rid.strip()][:100]
        if not ids:
            return {"costs": {}}

        def _query() -> dict[str, Any]:
            from urllib.parse import urlparse

            import clickhouse_connect

            url = os.environ.get("NOVA_CLICKHOUSE_URL", "")
            p = urlparse(url)
            client = clickhouse_connect.get_client(
                host=p.hostname or "localhost",
                port=p.port or 8123,
                username=p.username or "default",
                password=p.password or "",
                database=(p.path or "/nova").lstrip("/") or "nova",
            )
            # Use positional IN list to avoid parameter-binding limitations
            placeholders = ",".join(f"{{run_id_{i}:String}}" for i in range(len(ids)))
            params = {f"run_id_{i}": rid for i, rid in enumerate(ids)}
            sql = (
                "SELECT run_id,"
                " sum(input_tokens) AS input_tokens,"
                " sum(output_tokens) AS output_tokens,"
                " sum(cost_usd) AS cost_usd,"
                " count() AS calls"
                " FROM nova.cost_events"
                f" WHERE run_id IN ({placeholders})"
                " GROUP BY run_id"
            )
            result = client.query(sql, parameters=params)
            costs: dict[str, Any] = {}
            for row in result.result_rows:
                costs[row[0]] = {
                    "input_tokens": int(row[1]),
                    "output_tokens": int(row[2]),
                    "cost_usd": round(float(row[3]), 6),
                    "calls": int(row[4]),
                }
            return costs

        try:
            costs = await asyncio.get_event_loop().run_in_executor(None, _query)
            return {"costs": costs}
        except Exception as exc:  # noqa: BLE001
            logger.warning("runs/cost-summary: ClickHouse query failed: %s", exc)
            return {"costs": {}, "error": str(exc)}

    # ---------- B-1: cursor-based search endpoint ----------

    @app.get("/api/runs/search", dependencies=[Depends(verify_token)])
    async def search_runs_cursor(
        cursor: str | None = Query(default=None, description="Opaque cursor from previous response"),  # noqa: E501
        limit: int = Query(default=50, ge=1, le=200),
        q: str | None = Query(default=None, description="Free-text search on run_id and command"),  # noqa: E501
        status: str | None = Query(default=None, description="Filter by run status"),
        since: str | None = Query(default=None),
        until: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Cursor-based run listing.

        The cursor encodes (created_at, run_id) from the last item of the
        previous page. Items are ordered newest-first. Pass the returned
        ``next_cursor`` value as ``cursor`` to fetch the next page.
        """
        from novafabric.registry.store import get_connection, init_schema  # noqa: PLC0415
        _conn = get_connection(db_path)
        init_schema(_conn)
        try:
            if count_cached_runs(_conn) > 0:
                # Fast path: decode cursor → offset, query the index.
                decoded = _decode_cursor(cursor)
                offset = 0
                if decoded is not None:
                    cursor_ts, cursor_id = decoded
                    # Count rows that come before the cursor (newest-first order).
                    offset = _conn.execute(
                        "SELECT COUNT(*) FROM runs_cache"
                        " WHERE created_at > ? OR (created_at = ? AND run_id > ?)",
                        (cursor_ts, cursor_ts, cursor_id),
                    ).fetchone()[0]
                page, total_approx = _query_runs_cache(
                    _conn, limit=limit, offset=offset,
                    since=since, until=until, status=status, q=q,
                )
                next_cursor: str | None = None
                if len(page) == limit:
                    last = page[-1]
                    next_cursor = _encode_cursor(
                        last.get("created_at") or "", last.get("run_id") or ""
                    )
                return {"items": page, "next_cursor": next_cursor, "total_approx": total_approx}
        finally:
            _conn.close()

        # Fallback: full disk scan (pre-index startup).
        summaries = list_run_summaries(capsule_dir)
        if since:
            summaries = [s for s in summaries if (s.get("created_at") or "") >= since]
        if until:
            summaries = [s for s in summaries if (s.get("created_at") or "") <= until]
        if status and status != "all":
            summaries = [s for s in summaries if s.get("status") == status]
        if q:
            q_lower = q.lower()
            summaries = [
                s for s in summaries
                if q_lower in " ".join(s.get("command") or []).lower()
                or q_lower in (s.get("run_id") or "").lower()
            ]
        summaries.sort(
            key=lambda s: (s.get("created_at") or "", s.get("run_id") or ""),
            reverse=True,
        )
        total_approx = len(summaries)
        decoded = _decode_cursor(cursor)
        if decoded is not None:
            cursor_ts, cursor_id = decoded
            start = 0
            for i, s in enumerate(summaries):
                ts = s.get("created_at") or ""
                rid = s.get("run_id") or ""
                if ts == cursor_ts and rid == cursor_id:
                    start = i + 1
                    break
                if ts < cursor_ts or (ts == cursor_ts and rid < cursor_id):
                    start = i
                    break
            summaries = summaries[start:]
        page = summaries[:limit]
        remaining = summaries[limit:]
        next_cursor = None
        if remaining and page:
            last = page[-1]
            next_cursor = _encode_cursor(
                last.get("created_at") or "", last.get("run_id") or ""
            )
        return {"items": page, "next_cursor": next_cursor, "total_approx": total_approx}

    # ---------- B-3: SSE stream endpoint ----------

    @app.get("/api/runs/stream")
    async def stream_runs(
        t: str | None = Query(default=None, alias="token"),
    ) -> StreamingResponse:
        """Server-Sent Events stream of new run summaries.

        Emits ``data: <run_json>\\n\\n`` whenever a new run capsule is detected.
        The token is validated inline (SSE cannot use streaming + Depends easily).
        """
        if not t or not _consteq(t, token):
            return StreamingResponse(
                iter([]),
                status_code=401,
                media_type="text/event-stream",
            )

        q = _run_bus.subscribe()

        async def event_generator() -> AsyncGenerator[str, None]:
            # Send initial heartbeat comment so the browser knows the connection is live
            yield ": connected\n\n"
            try:
                while True:
                    try:
                        run = await asyncio.wait_for(q.get(), timeout=15.0)
                        payload = json.dumps(run, separators=(",", ":"))
                        yield f"data: {payload}\n\n"
                    except asyncio.TimeoutError:
                        # Heartbeat to keep the connection alive
                        yield ": heartbeat\n\n"
            finally:
                _run_bus.unsubscribe(q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/runs/{run_id}", dependencies=[Depends(verify_token)])
    async def get_run(run_id: str) -> dict[str, Any]:
        cdir = _resolve_capsule(run_id, capsule_dir)
        return load_full_capsule(cdir)

    @app.get("/api/runs/{run_id}/file/{filepath:path}", dependencies=[Depends(verify_token)])
    async def get_run_file(run_id: str, filepath: str) -> Any:
        parts = filepath.split("/")
        if len(parts) == 1:
            if ".." in parts[0] or parts[0].startswith("."):
                raise HTTPException(status_code=400, detail="invalid filename")
        elif len(parts) == 2:
            if parts[0] not in ("inputs", "outputs"):
                raise HTTPException(
                    status_code=400,
                    detail="only inputs/ and outputs/ subdirectories are accessible",
                )
            if not parts[1] or ".." in parts[1] or parts[1].startswith("."):
                raise HTTPException(status_code=400, detail="invalid filename")
        else:
            raise HTTPException(status_code=400, detail="path too deep")
        cdir = _resolve_capsule(run_id, capsule_dir)
        path = cdir / filepath
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        if filepath.endswith(".jsonl"):
            return {"filename": filepath, "lines": load_jsonl(cdir, filepath)}
        return {"filename": filepath, "content": path.read_text(errors="replace")}

    @app.get("/api/runs/{run_id}/energy", dependencies=[Depends(verify_token)])
    async def get_run_energy(run_id: str) -> dict[str, Any]:
        """Energy-Anchored Action Receipts + conservation for a run (ADR-0093)."""
        from novafabric.energy._attribution import load_receipts
        from novafabric.energy._conservation import compute_conservation

        cdir = _resolve_capsule(run_id, capsule_dir)
        receipts = load_receipts(cdir)
        if not receipts:
            return {"receipts": [], "conservation": None}
        conservation = compute_conservation(receipts, run_id=receipts[0].run_id)
        return {
            "receipts": [r.to_record() for r in receipts],
            "conservation": conservation.model_dump(),
        }

    @app.get("/api/runs/{run_id}/ledger", dependencies=[Depends(verify_token)])
    async def get_run_ledger(run_id: str) -> dict[str, Any]:
        """Adversary-anchored ledger verification status (ADR-0094)."""
        from novafabric.trust.ledger._verify import exit_code_for, verify_ledger

        cdir = _resolve_capsule(run_id, capsule_dir)
        verdict = verify_ledger(cdir)
        return {
            "ok": verdict.ok,
            "status": verdict.exit.name,
            "exit_code": exit_code_for(verdict),
            "reasons": verdict.reasons,
            "stream_verdicts": verdict.stream_verdicts,
        }

    @app.get("/api/runs/{run_id}/safety-case", dependencies=[Depends(verify_token)])
    async def get_run_safety_case(
        run_id: str, template: str = "clymer-generic-v0"
    ) -> dict[str, Any]:
        """Compile an evidence-grounded safety case for a run (ADR-0095)."""
        from novafabric.safetycase.compiler import SafetyCaseCompiler
        from novafabric.safetycase.templates import TemplateError

        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            case = SafetyCaseCompiler().build(cdir, template)
        except TemplateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return case.model_dump(mode="json")

    @app.post("/api/runs/{run_id}/verify", dependencies=[Depends(verify_token)])
    async def capsule_verify_endpoint(run_id: str) -> dict[str, Any]:
        """Verify DSSE signature + RFC 3161 timestamp + Merkle log inclusion (nova verify).

        Returns:
          sealed=False  — capsule has no .seal/ directory (was not sealed)
          configured=False — NovaSeal config not found
          signature_ok, timestamp_ok, log_integrity_ok, valid, errors — full result
        """
        import json as _json

        cdir = _resolve_capsule(run_id, capsule_dir)
        seal_dir = cdir / ".seal"
        if not seal_dir.exists():
            return {
                "sealed": False,
                "configured": None,
                "message": "No .seal/ directory — capsule was not sealed with NovaSeal.",
            }

        try:
            from novafabric.trust.novaseal.config import load_signing_profile
            profile = load_signing_profile()
        except Exception:
            profile = None

        if profile is None:
            return {
                "sealed": True,
                "configured": False,
                "message": "NovaSeal not configured — set NOVAFABRIC_SEAL_CONFIG or novaseal.yaml.",
            }

        from novafabric.trust.novaseal import KeyConfig, NovaSeal
        config = KeyConfig(
            profile=profile.profile,
            key_path=str(profile.key_path),
            cert_path=str(profile.cert_path),
        )
        seal = NovaSeal(config=config, tsa_url=profile.tsa_url, db_path=str(profile.merkle_db))

        log_file = seal_dir / "log-entry.json"
        capsule_id = ""
        if log_file.exists():
            try:
                entry = _json.loads(log_file.read_bytes())
                capsule_id = entry.get("entry", {}).get("capsule_id", "")
            except Exception:
                pass

        try:
            result = seal.verify(capsule_id=capsule_id, seal_dir=str(seal_dir))
        except Exception as exc:
            return {
                "sealed": True,
                "configured": True,
                "capsule_id": capsule_id,
                "signature_ok": False,
                "timestamp_ok": False,
                "log_integrity_ok": False,
                "valid": False,
                "errors": [str(exc)],
            }

        return {
            "sealed": True,
            "configured": True,
            "capsule_id": capsule_id,
            "signature_ok": result.signature_ok,
            "timestamp_ok": result.timestamp_ok,
            "log_integrity_ok": result.log_integrity_ok,
            "valid": result.valid,
            "errors": list(result.errors),
        }

    @app.get("/api/assets", dependencies=[Depends(verify_token)])
    async def list_assets_endpoint(
        type: str | None = Query(default=None, alias="type"),
        status_filter: str | None = Query(default=None, alias="status"),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        from novafabric.registry.service import (
            list_assets_paginated as _list_assets_paginated,
        )

        page, total = _list_assets_paginated(
            type, status_filter, limit=limit, offset=offset, db_path=db_path
        )
        return {
            "count": total,
            "total": total,
            "has_more": offset + limit < total,
            "limit": limit,
            "offset": offset,
            "assets": [_strip_spec(r) for r in page],
        }

    @app.get("/api/assets/{asset_id}", dependencies=[Depends(verify_token)])
    async def get_asset_by_id_endpoint(asset_id: str) -> dict[str, Any]:
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        try:
            row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail=f"Asset '{asset_id}' not found.")
            asset = dict(row)
            evals = conn.execute(
                "SELECT suite_name, passed, score_json, run_at "
                "FROM eval_results WHERE asset_id = ? ORDER BY run_at DESC",
                (asset_id,),
            ).fetchall()
            asset["eval_results"] = [
                {"suite_name": e["suite_name"], "passed": bool(e["passed"]),
                 "score": e["score_json"], "run_at": e["run_at"]}
                for e in evals
            ]
        finally:
            conn.close()
        return asset

    # NOTE: specific-path routes ({asset_id}/eval-history, {name}/diff) must be
    # registered before the catch-all {name}/{version} to prevent FastAPI from
    # matching e.g. /api/assets/foo/diff as name=foo, version=diff.
    @app.get("/api/assets/{name}/diff", dependencies=[Depends(verify_token)])
    async def asset_spec_diff_endpoint(
        name: str,
        from_version: str = Query(..., alias="from_version"),
        to_version: str = Query(..., alias="to_version"),
    ) -> dict[str, Any]:
        import json as _json

        # Path-traversal guard — reject any name or version containing / or ..
        for param, val in (
            ("name", name),
            ("from_version", from_version),
            ("to_version", to_version),
        ):
            if "/" in val or ".." in val:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid {param}: must not contain '/' or '..'",
                )

        from novafabric.registry.service import AssetNotFoundError
        from novafabric.registry.service import get_asset as _get_asset

        def _flatten_spec(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
            result: dict[str, Any] = {}
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    result.update(_flatten_spec(v, key))
                else:
                    result[key] = v
            return result

        try:
            asset_from = _get_asset(name, from_version, db_path=db_path)
        except AssetNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        try:
            asset_to = _get_asset(name, to_version, db_path=db_path)
        except AssetNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        spec_from = _flatten_spec(_json.loads(asset_from.get("spec_json", "{}")))
        spec_to = _flatten_spec(_json.loads(asset_to.get("spec_json", "{}")))

        all_keys = sorted(set(spec_from) | set(spec_to))
        changed: list[dict[str, Any]] = []
        added: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []

        for k in all_keys:
            in_from = k in spec_from
            in_to = k in spec_to
            if in_from and in_to:
                if spec_from[k] != spec_to[k]:
                    changed.append({"key": k, "from": spec_from[k], "to": spec_to[k]})
            elif in_to:
                added.append({"key": k, "value": spec_to[k]})
            else:
                removed.append({"key": k, "value": spec_from[k]})

        return {
            "name": name,
            "from_version": from_version,
            "to_version": to_version,
            "changed": changed,
            "added": added,
            "removed": removed,
            "identical": len(changed) == 0 and len(added) == 0 and len(removed) == 0,
        }

    @app.get("/api/assets/{asset_id}/eval-history", dependencies=[Depends(verify_token)])
    async def eval_history_endpoint(
        asset_id: str,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        try:
            # Verify asset exists
            exists = conn.execute(
                "SELECT 1 FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if not exists:
                raise HTTPException(status_code=404, detail=f"Asset '{asset_id}' not found.")

            rows = conn.execute(
                "SELECT id, suite_name, passed, score_json, run_at "
                "FROM eval_results WHERE asset_id = ? ORDER BY run_at DESC LIMIT ?",
                (asset_id, limit),
            ).fetchall()
        finally:
            conn.close()

        return {
            "asset_id": asset_id,
            "history": [
                {
                    "eval_id": str(row["id"]),
                    "suite_name": row["suite_name"],
                    "passed": bool(row["passed"]),
                    "score": _extract_score(row["score_json"]),
                    "run_at": row["run_at"],
                }
                for row in rows
            ],
        }

    # NOTE: /api/assets/{asset_id}/approvals must be registered before the
    # catch-all {name}/{version} route to prevent FastAPI matching "approvals"
    # as the version segment.
    @app.get("/api/assets/{asset_id}/approvals", dependencies=[Depends(verify_token)])
    async def get_approvals_endpoint(asset_id: str) -> dict[str, Any]:
        """Return approval records for an asset (by UUID)."""
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        try:
            # Resolve asset_id to name+version
            row = conn.execute(
                "SELECT name, version FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
            if not row:
                return {
                    "asset_id": asset_id,
                    "approvals": [],
                    "required": 1,
                    "supported": True,
                }
            name, version = row["name"], row["version"]
            rows = conn.execute(
                """
                SELECT approver, approved_at, note FROM approvals
                WHERE asset_name = ? AND asset_version = ?
                ORDER BY approved_at ASC
                """,
                (name, version),
            ).fetchall()
        finally:
            conn.close()

        return {
            "asset_id": asset_id,
            "approvals": [
                {
                    "role": "reviewer",
                    "actor": r["approver"],
                    "note": r["note"],
                    "approved_at": r["approved_at"],
                }
                for r in rows
            ],
            "required": 1,
            "supported": True,
        }

    @app.get("/api/assets/{name}/{version}", dependencies=[Depends(verify_token)])
    async def get_asset_endpoint(name: str, version: str) -> dict[str, Any]:
        from novafabric.registry.service import AssetNotFoundError
        from novafabric.registry.service import get_asset as _get_asset
        from novafabric.registry.store import get_connection

        try:
            asset = _get_asset(name, version, db_path=db_path)
        except AssetNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))

        # Pull eval results too.
        conn = get_connection(db_path)
        try:
            evals = conn.execute(
                "SELECT suite_name, passed, score_json, run_at "
                "FROM eval_results WHERE asset_id = ? ORDER BY run_at DESC",
                (asset["id"],),
            ).fetchall()
            asset["eval_results"] = [
                {
                    "suite_name": e["suite_name"],
                    "passed": bool(e["passed"]),
                    "score": e["score_json"],
                    "run_at": e["run_at"],
                }
                for e in evals
            ]
        finally:
            conn.close()
        return asset

    # ---------- Evidence list / detail (GET — read-only) ----------

    @app.get("/api/evidence", dependencies=[Depends(verify_token)])
    async def list_evidence_endpoint() -> dict[str, Any]:
        bundles = _list_evidence_bundles()
        return {"bundles": bundles, "count": len(bundles)}

    @app.get("/api/evidence/{bundle_id}/download", dependencies=[Depends(verify_token)])
    async def download_evidence_endpoint(bundle_id: str) -> Any:
        from fastapi.responses import FileResponse

        override = os.environ.get("NOVAFABRIC_EVIDENCE_DIR")
        evidence_dir = Path(override) if override else Path.home() / ".novafabric" / "evidence"
        zip_path = evidence_dir / f"{bundle_id}.zip"
        if not zip_path.exists():
            raise HTTPException(status_code=404, detail=f"Evidence bundle '{bundle_id}' not found.")
        return FileResponse(
            path=str(zip_path),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="evidence-{bundle_id}.zip"'},
        )

    @app.get("/api/evidence/{bundle_id}", dependencies=[Depends(verify_token)])
    async def get_evidence_detail_endpoint(bundle_id: str) -> dict[str, Any]:
        import base64
        import hashlib
        import json as _json
        import zipfile

        override = os.environ.get("NOVAFABRIC_EVIDENCE_DIR")
        evidence_dir = Path(override) if override else Path.home() / ".novafabric" / "evidence"
        zip_path = evidence_dir / f"{bundle_id}.zip"
        if not zip_path.exists():
            raise HTTPException(status_code=404, detail=f"Evidence bundle '{bundle_id}' not found.")

        size_bytes = zip_path.stat().st_size
        manifest: dict[str, Any] = {}
        dsse_statement: dict[str, Any] = {}
        dsse_envelope: dict[str, Any] = {}
        signing_key_fingerprint: str | None = None
        files: list[dict[str, Any]] = []

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()

                # Manifest
                if "manifest.json" in names:
                    manifest = _json.loads(zf.read("manifest.json"))

                # DSSE envelope
                dsse_path = "attestations/run.intoto.json"
                if dsse_path in names:
                    envelope_raw = _json.loads(zf.read(dsse_path))
                    dsse_envelope = envelope_raw
                    # Decode payload field (base64)
                    payload_b64 = envelope_raw.get("payload", "")
                    if payload_b64:
                        try:
                            dsse_statement = _json.loads(base64.b64decode(payload_b64 + "=="))
                        except Exception:  # noqa: BLE001
                            dsse_statement = {}

                # Signing key fingerprint from cert
                cert_path = "signatures/run.intoto.json.cert"
                if cert_path in names:
                    cert_bytes = zf.read(cert_path)
                    signing_key_fingerprint = hashlib.sha256(cert_bytes).hexdigest()[:16]

                # File list
                files = sorted(
                    [
                        {"path": info.filename, "size_bytes": info.compress_size}
                        for info in zf.infolist()
                    ],
                    key=lambda f: f["path"],
                )
        except zipfile.BadZipFile:
            raise HTTPException(status_code=422, detail=f"Bundle '{bundle_id}' is not a valid ZIP.")

        run_id = manifest.get("run_id", bundle_id)
        timestamp = manifest.get("created_at") or manifest.get("timestamp") or ""

        return {
            "bundle_id": bundle_id,
            "run_id": run_id,
            "timestamp": timestamp,
            "size_bytes": size_bytes,
            "manifest": manifest,
            "dsse_statement": dsse_statement,
            "dsse_envelope": dsse_envelope,
            "signing_key_fingerprint": signing_key_fingerprint,
            "files": files,
        }

    @app.post("/api/evidence/{bundle_id}/verify", dependencies=[Depends(verify_token)])
    async def verify_evidence_endpoint(bundle_id: str) -> dict[str, Any]:
        """Verify the cryptographic integrity of an evidence bundle.

        Checks (in order):
          1. signature_ok   — Ed25519 DSSE signature on attestations/run.intoto.json
          2. timestamp_ok   — RFC 3161 TSR in manifest.dsse.tsr (null if not present)
          3. log_integrity_ok — NovaSeal Merkle inclusion (null if not configured)
        """
        import json as _json
        import zipfile

        if "/" in bundle_id or ".." in bundle_id:
            raise HTTPException(status_code=400, detail="invalid bundle_id")

        override = os.environ.get("NOVAFABRIC_EVIDENCE_DIR")
        evidence_dir = Path(override) if override else Path.home() / ".novafabric" / "evidence"
        zip_path = evidence_dir / f"{bundle_id}.zip"
        if not zip_path.exists():
            raise HTTPException(status_code=404, detail=f"Evidence bundle '{bundle_id}' not found.")

        errors: list[str] = []
        signature_ok = False
        timestamp_ok: bool | None = None
        log_integrity_ok: bool | None = None
        seal_available = False
        run_id = bundle_id

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                manifest: dict[str, Any] = {}
                if "manifest.json" in names:
                    manifest = _json.loads(zf.read("manifest.json"))
                    run_id = manifest.get("run_id", bundle_id)

                # --- 1. Ed25519 DSSE signature ---
                dsse_bytes = b""
                dsse_path = "attestations/run.intoto.json"
                _cert_candidates = ("signatures/run.cert", "signatures/run.intoto.json.cert")
                cert_path_in_zip = next((p for p in _cert_candidates if p in names), None)
                if dsse_path not in names:
                    errors.append(f"Missing {dsse_path} in bundle")
                elif cert_path_in_zip is None:
                    errors.append("Missing signing certificate in bundle (signatures/run.cert)")
                else:
                    dsse_bytes = zf.read(dsse_path)
                    cert_pem = zf.read(cert_path_in_zip)
                    try:
                        from novafabric.evidence.intoto import dsse_verify
                        from novafabric.evidence.signing import verify_with_pem
                        envelope = _json.loads(dsse_bytes)
                        dsse_verify(envelope, lambda pae, sig: verify_with_pem(cert_pem, pae, sig))
                        signature_ok = True
                    except Exception as exc:
                        errors.append(f"DSSE signature verification failed: {exc}")

                # --- 2. RFC 3161 TSR ---
                tsr_path = "manifest.dsse.tsr"
                if tsr_path in names:
                    tsr_bytes = zf.read(tsr_path)
                    if not tsr_bytes:
                        timestamp_ok = True  # empty TSR = TSA skipped intentionally
                    elif dsse_bytes:
                        try:
                            from novafabric.trust.novaseal.timestamp import verify_timestamp
                            timestamp_ok = verify_timestamp(tsr_bytes, dsse_bytes)
                            if not timestamp_ok:
                                errors.append("RFC 3161 TSR verification failed: hash mismatch")
                        except Exception as exc:
                            timestamp_ok = False
                            errors.append(f"TSR verification error: {exc}")
                    else:
                        timestamp_ok = False
                        errors.append("Cannot verify TSR: DSSE envelope missing")
                else:
                    ts_status = manifest.get("timestamp_status", "")
                    if ts_status == "failed":
                        timestamp_ok = False
                        errors.append("Timestamp was not obtained during export (TSA error)")
                    # else: not requested — null = not applicable

        except zipfile.BadZipFile:
            raise HTTPException(status_code=422, detail=f"Bundle '{bundle_id}' is not a valid ZIP.")

        # --- 3. NovaSeal Merkle log (optional, requires local capsule + config) ---
        try:
            from novafabric.trust.novaseal.config import load_signing_profile
            profile = load_signing_profile()
            if profile is not None:
                seal_available = True
                try:
                    cdir = _resolve_capsule(run_id, capsule_dir)
                except HTTPException:
                    cdir = None
                if cdir is not None:
                    seal_dir = cdir / ".seal"
                    if seal_dir.is_dir():
                        from novafabric.trust.novaseal import KeyConfig, NovaSeal  # noqa: PLC0415
                        config = KeyConfig(
                            profile=profile.profile,
                            key_path=str(profile.key_path),
                            cert_path=str(profile.cert_path),
                        )
                        seal = NovaSeal(
                            config=config, tsa_url=profile.tsa_url, db_path=str(profile.merkle_db)
                        )
                        log_file = seal_dir / "log-entry.json"
                        capsule_id = ""
                        if log_file.exists():
                            entry = _json.loads(log_file.read_bytes())
                            capsule_id = entry.get("entry", {}).get("capsule_id", "")
                        seal_result = seal.verify(capsule_id=capsule_id, seal_dir=str(seal_dir))
                        log_integrity_ok = seal_result.log_integrity_ok
                        if not seal_result.log_integrity_ok:
                            errors.extend(seal_result.errors)
        except Exception:
            pass  # NovaSeal not configured or unavailable

        valid = signature_ok and (timestamp_ok is not False) and (log_integrity_ok is not False)
        return {
            "bundle_id": bundle_id,
            "run_id": run_id,
            "valid": valid,
            "signature_ok": signature_ok,
            "timestamp_ok": timestamp_ok,
            "log_integrity_ok": log_integrity_ok,
            "seal_available": seal_available,
            "errors": errors,
        }

    @app.get("/api/lineage/provenance/{ref:path}", dependencies=[Depends(verify_token)])
    async def lineage_provenance(
        ref: str, depth: int = Query(default=5, ge=1, le=20), kind: str | None = None,
    ) -> dict[str, Any]:
        from novafabric.lineage._store import LineageStore

        store = LineageStore(db_path=db_path)
        return {"ref": ref, "depth": depth, "ancestors": store.provenance(ref, kind, depth)}

    @app.get("/api/lineage/blast-radius/{ref:path}", dependencies=[Depends(verify_token)])
    async def lineage_blast_radius(
        ref: str, depth: int = Query(default=5, ge=1, le=20), kind: str | None = None,
    ) -> dict[str, Any]:
        from novafabric.lineage._store import LineageStore

        store = LineageStore(db_path=db_path)
        return {"ref": ref, "depth": depth, "descendants": store.blast_radius(ref, kind, depth)}

    @app.get("/api/lineage/replay-chain/{run_id}", dependencies=[Depends(verify_token)])
    async def lineage_replay_chain(run_id: str) -> dict[str, Any]:
        from novafabric.lineage._store import LineageStore

        store = LineageStore(db_path=db_path)
        return {"run_id": run_id, "chain": store.replay_chain(run_id)}

    @app.get("/api/lineage/time-travel/{ref:path}", dependencies=[Depends(verify_token)])
    async def lineage_time_travel(
        ref: str,
        at: str = Query(..., description="ISO-8601 timestamp"),
        depth: int = Query(default=5, ge=1, le=20),
    ) -> dict[str, Any]:
        from novafabric.lineage._store import LineageStore

        store = LineageStore(db_path=db_path)
        # time_travel may not be implemented on all backends; catch AttributeError.
        # The SQLite backend supports a limited temporal query: returns edges whose
        # created_at <= `at`.  Pass `at` as the `asof` argument.
        try:
            ancestors = store.time_travel(ref, asof=at)
        except AttributeError:
            ancestors = None  # backend doesn't support it
        return {
            "ref": ref,
            "at": at,
            "depth": depth,
            "supported": ancestors is not None,
            "ancestors": ancestors or [],
        }

    @app.get("/api/lineage/edges", dependencies=[Depends(verify_token)])
    async def lineage_edges(
        limit: int = Query(default=2000, ge=1, le=20000),
    ) -> dict[str, Any]:
        """Return every lineage edge in the SQLite. Powers the dashboard's full-DAG view."""
        import json

        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        try:
            # Lineage tables are created by LineageStore on first use; check if they exist.
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "lineage_edges" not in tables:
                return {"count": 0, "edges": []}

            rows = conn.execute(
                """
                SELECT e.edge_id, e.edge_type, e.capsule_run_id,  -- noqa
                       e.confidence, e.created_at, e.payload,
                       ns.kind AS source_kind, ns.ref AS source_ref,
                       nt.kind AS target_kind, nt.ref AS target_ref
                FROM lineage_edges e
                JOIN lineage_nodes ns ON ns.node_id = e.source_id
                JOIN lineage_nodes nt ON nt.node_id = e.target_id
                ORDER BY e.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

            edges = []
            for r in rows:
                payload = {}
                try:
                    payload = json.loads(r["payload"] or "{}")
                except (ValueError, TypeError):
                    pass
                edges.append({
                    "edge_id": r["edge_id"],
                    "edge_type": r["edge_type"],
                    "source": f"{r['source_kind']}:{r['source_ref']}",
                    "target": f"{r['target_kind']}:{r['target_ref']}",
                    "capsule_run_id": r["capsule_run_id"],
                    "confidence": r["confidence"] or "observed",
                    "created_at": r["created_at"],
                    "direction": "source_to_target",
                    "schema_version": payload.get("schema_version", "0.1.0"),
                    "emitter": payload.get(
                        "emitter", {"name": "novafabric.lineage", "version": "unknown"}
                    ),
                })
            return {"count": len(edges), "edges": edges}
        finally:
            conn.close()

    @app.post("/api/lineage/import", dependencies=[Depends(verify_token)])
    async def lineage_import_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Import capsule lineage events into the store — mirrors `nova lineage import`."""
        capsule_path_str: str = body.get("capsule_path", "")
        if not capsule_path_str:
            raise HTTPException(status_code=422, detail="capsule_path is required")
        if not capsule_path_str.startswith("/") and "/" not in capsule_path_str:
            # bare run_id — resolve to capsule directory
            cap_dir = capsule_dir / capsule_path_str
        else:
            cap_dir = Path(capsule_path_str)
        if not cap_dir.is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"Capsule directory not found: {capsule_path_str}",
            )
        try:
            from novafabric.lineage._importer import import_capsule_dir  # noqa: PLC0415
            results = import_capsule_dir(cap_dir)
            imported = sum(r.edges_indexed for r in results)
            skipped = sum(1 for r in results if r.skipped)
            return {
                "ok": True,
                "capsule_path": capsule_path_str,
                "imported": imported,
                "skipped": skipped,
                "file_count": len(results),
                "note": f"Imported {imported} lineage event(s) from {len(results)} capsule(s)",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "capsule_path": capsule_path_str,
                "imported": 0,
                "skipped": 0,
                "note": str(exc),
            }

    @app.get("/api/lineage/{run_id}/emit-openlineage", dependencies=[Depends(verify_token)])
    async def lineage_emit_openlineage_endpoint(run_id: str) -> dict[str, Any]:
        """Return OpenLineage events for a capsule as JSON (nova lineage emit-openlineage)."""
        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            from novafabric.lineage._openlineage import build_events_from_capsule  # noqa: PLC0415
            events = build_events_from_capsule(cdir)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "run_id": run_id,
                "event_count": 0,
                "events": [],
                "error": str(exc),
            }
        return {"ok": True, "run_id": run_id, "event_count": len(events), "events": events}

    # ---------- OTLP GenAI ingest (NF-034, ADR-0098; experimental) ----------

    @app.post("/api/otlp/v1/traces", dependencies=[Depends(verify_token)])
    async def otlp_ingest_traces(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Ingest OTLP/HTTP JSON OTel GenAI spans into a run capsule (NF-034, experimental).

        Accepts an ExportTraceServiceRequest-shaped JSON body, converts spans
        carrying gen_ai.* attributes into capsule events, and seals a capsule
        with capture_level 'ingested-otlp'. Spans without gen_ai.* attributes
        are skipped (counted, never guessed). Protobuf bodies are not accepted
        in this slice — OTLP JSON only.
        """
        from novafabric.otel.genai_ingest import (  # noqa: PLC0415
            CAPTURE_LEVEL,
            OTLPIngestError,
            ingest_otlp_json,
            write_ingest_capsule,
        )

        try:
            result = ingest_otlp_json(body)
        except OTLPIngestError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if result.genai_spans == 0:
            return {
                "capsule_id": None,
                "spans_ingested": 0,
                "spans_skipped": result.skipped_spans + result.unclassified_spans,
                "capture_level": CAPTURE_LEVEL,
                "note": "no OTel GenAI spans in payload; no capsule written",
            }

        cdir = write_ingest_capsule(result, capsule_dir)
        return {
            "capsule_id": cdir.name,
            "spans_ingested": result.genai_spans,
            "spans_skipped": result.skipped_spans + result.unclassified_spans,
            "model_call_count": len(result.model_calls),
            "tool_call_count": len(result.tool_calls),
            "capture_level": CAPTURE_LEVEL,
            "unmapped_attribute_keys": result.unmapped_keys,
        }

    @app.get("/api/diff", dependencies=[Depends(verify_token)])
    async def diff_runs(
        run_a: str = Query(...),
        run_b: str = Query(...),
    ) -> dict[str, Any]:
        cdir_a = _resolve_capsule(run_a, capsule_dir)
        cdir_b = _resolve_capsule(run_b, capsule_dir)
        try:
            from novafabric.diff._engine import DiffEngine
        except (ImportError, ModuleNotFoundError):
            raise HTTPException(status_code=501, detail="diff engine unavailable")
        engine = DiffEngine()
        report = engine.compare(cdir_a, cdir_b)
        # DiffEngine returns a Pydantic model in current code; serialize.
        if hasattr(report, "model_dump"):
            return report.model_dump(mode="json")  # type: ignore[no-any-return]
        return {"run_a_id": run_a, "run_b_id": run_b, "report": report}

    # ---------- audit log ----------

    @app.get("/api/audit", dependencies=[Depends(verify_token)])
    async def list_audit(limit: int = Query(default=200, ge=1, le=2000)) -> dict[str, Any]:
        entries = audit.read_recent(limit)
        return {"count": len(entries), "entries": entries}

    # ---------- legal holds (ADR-0031) ----------

    def _holds_base() -> Path:
        return capsule_dir.parent / "registries"

    def _read_holds_file(registry: str) -> list[dict[str, Any]]:
        path = _holds_base() / registry / "holds.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text().splitlines()
            if line.strip()
        ]

    def _active_holds(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [h for h in records if h.get("released_at") is None]

    @app.get("/api/holds", dependencies=[Depends(verify_token)])
    async def list_holds() -> dict[str, Any]:
        base = _holds_base()
        registries: list[dict[str, Any]] = []
        total_active = 0
        if base.exists():
            for reg_dir in sorted(base.iterdir()):
                if not reg_dir.is_dir():
                    continue
                all_records = _read_holds_file(reg_dir.name)
                active = _active_holds(all_records)
                total_active += len(active)
                registries.append({"name": reg_dir.name, "holds": active})
        return {"total_active": total_active, "registries": registries}

    @app.post("/api/holds", dependencies=[Depends(verify_token)])
    async def create_hold(body: PlaceHoldRequest = Body(...)) -> dict[str, Any]:
        import uuid as _uuid
        from datetime import datetime, timezone

        for param, val in (("registry", body.registry),):
            if "/" in val or ".." in val:
                raise HTTPException(
                    status_code=400,
                    detail=f"invalid {param}: must not contain '/' or '..'",
                )
        if not body.registry.strip():
            raise HTTPException(status_code=422, detail="registry must not be empty")
        if not body.reason.strip():
            raise HTTPException(status_code=422, detail="reason must not be empty")

        hold_id = f"hold-{_uuid.uuid4().hex[:8]}"
        path = _holds_base() / body.registry / "holds.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record: dict[str, Any] = {
            "hold_id": hold_id,
            "registry": body.registry,
            "reason": body.reason,
            "duration_days": body.duration_days,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "released_at": None,
        }
        with path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return {k: v for k, v in record.items() if k != "released_at"}

    @app.post("/api/holds/{hold_id}/release", dependencies=[Depends(verify_token)])
    async def release_hold(hold_id: str) -> dict[str, Any]:
        import os as _os
        from datetime import datetime, timezone

        if "/" in hold_id or ".." in hold_id:
            raise HTTPException(status_code=400, detail="invalid hold_id")

        base = _holds_base()
        if not base.exists():
            raise HTTPException(status_code=404, detail=f"hold not found: {hold_id}")

        for reg_dir in base.iterdir():
            if not reg_dir.is_dir():
                continue
            holds_file = reg_dir / "holds.jsonl"
            if not holds_file.exists():
                continue
            lines = holds_file.read_text().splitlines()
            updated: list[str] = []
            registry = reg_dir.name
            released = False
            for line in lines:
                if not line.strip():
                    continue
                h = json.loads(line)
                if h["hold_id"] == hold_id and h["released_at"] is None:
                    h["released_at"] = datetime.now(tz=timezone.utc).isoformat()
                    released = True
                updated.append(json.dumps(h))
            if released:
                tmp = holds_file.with_suffix(".tmp")
                tmp.write_text("\n".join(updated) + "\n")
                _os.replace(tmp, holds_file)
                return {"released": True, "hold_id": hold_id, "registry": registry}

        raise HTTPException(
            status_code=404, detail=f"hold not found or already released: {hold_id}"
        )

    # ---------- Layer B mutations (per ADR-0027 §1) ----------
    # Safe mutations only: registry writes, eval runs, evidence exports.
    # These never spawn user-supplied code. Layer C (control plane / capture)
    # remains gated by additional review.

    @app.post("/api/assets")
    async def register_asset_endpoint(
        body: RegisterAssetRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        import tempfile

        from novafabric.registry.service import DuplicateAssetError, register_asset
        from novafabric.spec.validator import SpecValidationError, validate_spec

        # Write the YAML to a tmp file so the existing validator (which expects a Path) can read it.
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            tmp.write(body.spec_yaml)
            tmp_path = Path(tmp.name)

        try:
            try:
                spec = validate_spec(tmp_path)
            except SpecValidationError as e:
                audit.append(
                    action="register_asset",
                    args={"spec_yaml_len": len(body.spec_yaml)},
                    cli_equivalent="nova register <spec.yaml>",
                    actor_token_fp=actor_fp,
                    result="error",
                    error=f"spec validation failed: {e}",
                )
                raise HTTPException(status_code=422, detail=f"spec validation failed: {e}")

            try:
                result = register_asset(spec, tmp_path, db_path=db_path)
            except DuplicateAssetError as e:
                audit.append(
                    action="register_asset",
                    args={
                        "name": getattr(spec, "name", "?"),
                        "version": getattr(spec, "version", "?"),
                    },
                    cli_equivalent=f"nova register <{getattr(spec, 'name', 'spec')}.yaml>",
                    actor_token_fp=actor_fp,
                    result="error",
                    error=str(e),
                )
                raise HTTPException(status_code=409, detail=str(e))
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

        audit.append(
            action="register_asset",
            args={"name": result.get("name"), "version": result.get("version")},
            cli_equivalent=f"nova register <{result.get('name')}.yaml>",
            actor_token_fp=actor_fp,
            extra={"asset_id": result.get("id")},
        )
        return {"ok": True, "asset": _strip_spec(result)}

    @app.post("/api/assets/register-from-yaml", dependencies=[Depends(verify_token)])
    async def register_asset_from_yaml_endpoint(
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Register an asset from a YAML spec string (used by suggest-register panel)."""
        spec_yaml = body.get("spec_yaml", "")
        if not spec_yaml.strip():
            raise HTTPException(status_code=422, detail="spec_yaml required")
        import tempfile as _tempfile

        from novafabric.registry.service import DuplicateAssetError, register_asset
        from novafabric.spec.validator import SpecValidationError, validate_spec

        try:
            with _tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
                f.write(spec_yaml)
                tmp = Path(f.name)
            try:
                spec = validate_spec(tmp)
                register_asset(spec, spec_path=tmp, db_path=db_path)
                return {"ok": True, "name": spec.name}
            finally:
                tmp.unlink(missing_ok=True)
        except SpecValidationError as exc:
            return {"ok": False, "error": str(exc)}
        except DuplicateAssetError:
            return {"ok": False, "error": "already registered"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @app.post("/api/assets/{asset_id}/eval")
    async def eval_asset_by_id_endpoint(
        asset_id: str,
        body: EvalRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Eval by UUID — resolves name+version from the registry then delegates."""
        if not body.confirmed:
            raise HTTPException(
                status_code=400, detail="confirmation required (set confirmed=true)"
            )
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        try:
            row = conn.execute(
                "SELECT name, version FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=404, detail=f"Asset '{asset_id}' not found.")
        return await eval_asset_endpoint(row["name"], row["version"], body, actor_fp)

    @app.post("/api/assets/{name}/{version}/eval")
    async def eval_asset_endpoint(
        name: str,
        version: str,
        body: EvalRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        from novafabric.eval.runner import run_evals
        from novafabric.registry.service import AssetNotFoundError

        try:
            result = run_evals(name, version, db_path=db_path)
        except AssetNotFoundError as e:
            audit.append(
                action="eval_asset",
                args={"name": name, "version": version},
                cli_equivalent=f"nova eval {name}@{version}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:  # noqa: BLE001 — surface eval errors to the user
            audit.append(
                action="eval_asset",
                args={"name": name, "version": version},
                cli_equivalent=f"nova eval {name}@{version}",
                actor_token_fp=actor_fp,
                result="error",
                error=repr(e),
            )
            raise HTTPException(status_code=500, detail=f"eval failed: {e}")

        audit.append(
            action="eval_asset",
            args={"name": name, "version": version},
            cli_equivalent=f"nova eval {name}@{version}",
            actor_token_fp=actor_fp,
            extra={"passed": result.get("passed")},
        )
        return {"ok": True, "result": result}

    @app.post("/api/eval/compare", dependencies=[Depends(verify_token)])
    async def eval_compare_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Compare two EvalResult JSON objects for regression — mirrors `nova eval compare`."""
        baseline_json: str = body.get("baseline_json", "")
        candidate_json: str = body.get("candidate_json", "")
        alpha: float = float(body.get("alpha", 0.05))
        min_samples: int = int(body.get("min_samples", 5))
        if not baseline_json.strip():
            raise HTTPException(status_code=422, detail="baseline_json is required")
        if not candidate_json.strip():
            raise HTTPException(status_code=422, detail="candidate_json is required")
        try:
            from novafabric.evals._stats import RegressionDetector  # noqa: PLC0415
            from novafabric.evals.result import EvalResult  # noqa: PLC0415
            baseline = EvalResult.model_validate_json(baseline_json)
            candidate = EvalResult.model_validate_json(candidate_json)
            detector = RegressionDetector(alpha=alpha, min_samples=min_samples)
            report = detector.compare(baseline=baseline, candidate=candidate)
            return {
                "ok": True,
                "suite_id": report.suite_id,
                "regression_detected": report.regression_detected,
                "summary": report.summary,
                "metrics": [m.model_dump() for m in report.metrics],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "regression_detected": False, "summary": str(exc), "metrics": []}

    @app.post("/api/evidence/{run_id}")
    async def export_evidence_endpoint(
        run_id: str,
        body: ExportEvidenceRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            from novafabric.evidence.bundle import (
                CapsuleValidationError,
                EvidenceBundleBuilder,
                UnsafeSkipsError,
            )
            from novafabric.evidence.signing import LocalSigner
        except ImportError as e:
            raise HTTPException(status_code=501, detail=f"evidence bundle module unavailable: {e}")

        key_pem_default = Path.home() / ".novafabric" / "keys" / "local-key.pem"
        key_path = Path(body.key_path) if body.key_path else key_pem_default
        key_was_autogenerated = False
        if not key_path.exists():
            if body.key_path:
                # User specified a custom path — don't second-guess it.
                audit.append(
                    action="export_evidence",
                    args={"run_id": run_id, "key_path": str(key_path)},
                    cli_equivalent=f"nova export-evidence {cdir}",
                    actor_token_fp=actor_fp,
                    result="error",
                    error=f"signing key not found at {key_path}",
                )
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Signing key not found at {key_path}."
                        " Generate it or correct `key_path`."
                    ),
                )
            # Default path is missing — auto-generate a fresh demo keypair.
            try:
                from novafabric.evidence.signing import (
                    generate_keypair,
                )
                priv_path, pub_path = generate_keypair(key_path.parent)
                # generate_keypair emits ed25519.pem; alias to local-key.pem so the default works.
                if priv_path != key_path:
                    key_path.write_bytes(priv_path.read_bytes())
                    try:
                        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
                    except OSError:
                        pass
                    pub_alias = key_path.with_suffix(".pub.pem")
                    if not pub_alias.exists():
                        pub_alias.write_bytes(pub_path.read_bytes())
                key_was_autogenerated = True
            except Exception as e:  # noqa: BLE001
                audit.append(
                    action="export_evidence",
                    args={"run_id": run_id, "key_path": str(key_path)},
                    cli_equivalent=f"nova export-evidence {cdir}",
                    actor_token_fp=actor_fp,
                    result="error",
                    error=f"signing key auto-generate failed: {e}",
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to auto-generate signing key at {key_path}: {e}",
                )

        out_default = Path.home() / ".novafabric" / "evidence" / f"{run_id}.zip"
        output_path = Path(body.output_path) if body.output_path else out_default
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            signer = LocalSigner(key_path)
            builder = EvidenceBundleBuilder(
                capsule_dir=cdir,
                signer=signer,
                output_path=output_path,
                allow_unsafe_skips=body.allow_unsafe_skips,
            )
            out = builder.build()
        except UnsafeSkipsError as e:
            audit.append(
                action="export_evidence",
                args={"run_id": run_id},
                cli_equivalent=f"nova export-evidence {cdir} --output {output_path}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=409, detail=str(e))
        except CapsuleValidationError as e:
            audit.append(
                action="export_evidence",
                args={"run_id": run_id},
                cli_equivalent=f"nova export-evidence {cdir} --output {output_path}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=422, detail=str(e))

        size = out.stat().st_size if out.exists() else 0
        extra: dict[str, Any] = {"size_bytes": size}
        if key_was_autogenerated:
            extra["key_autogenerated_at"] = str(key_path)
        audit.append(
            action="export_evidence",
            args={"run_id": run_id, "output_path": str(out)},
            cli_equivalent=f"nova export-evidence {cdir} --output {output_path} --key {key_path}",
            actor_token_fp=actor_fp,
            extra=extra,
        )
        return {
            "ok": True,
            "bundle_path": str(out),
            "size_bytes": size,
            "key_autogenerated": key_was_autogenerated,
        }

    # ---------- Layer B: Promote ----------

    @app.post("/api/assets/{asset_id}/promote")
    async def promote_asset_by_id_endpoint(
        asset_id: str,
        body: PromoteRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Promote by UUID — resolves name+version from the registry then delegates."""
        if not body.confirmed:
            raise HTTPException(
                status_code=400, detail="confirmation required (set confirmed=true)"
            )
        from novafabric.registry.store import get_connection, init_schema

        conn = get_connection(db_path)
        init_schema(conn)
        try:
            row = conn.execute(
                "SELECT name, version FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
        finally:
            conn.close()
        if not row:
            raise HTTPException(status_code=404, detail=f"Asset '{asset_id}' not found.")
        return await promote_asset_endpoint(row["name"], row["version"], body, actor_fp)

    @app.post("/api/assets/{name}/{version}/promote")
    async def promote_asset_endpoint(
        name: str,
        version: str,
        body: PromoteRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        from novafabric.registry.service import (
            AssetNotFoundError,
            InvalidLifecycleTransitionError,
            PromotionBlockedError,
            promote_asset,
        )
        from novafabric.spec.models import AssetStatus

        try:
            target = AssetStatus(body.to_status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"invalid to_status: {body.to_status}")

        try:
            result = promote_asset(
                name=name,
                version=version,
                to_status=target,
                actor=body.actor,
                force=body.force,
                db_path=db_path,
            )
        except AssetNotFoundError as e:
            audit.append(
                action="promote_asset",
                args={"name": name, "version": version, "to_status": body.to_status},
                cli_equivalent=f"nova promote {name}@{version} --to {body.to_status}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=404, detail=str(e))
        except InvalidLifecycleTransitionError as e:
            audit.append(
                action="promote_asset",
                args={"name": name, "version": version, "to_status": body.to_status},
                cli_equivalent=f"nova promote {name}@{version} --to {body.to_status}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=409, detail=str(e))
        except PromotionBlockedError as e:
            audit.append(
                action="promote_asset",
                args={"name": name, "version": version, "to_status": body.to_status},
                cli_equivalent=f"nova promote {name}@{version} --to {body.to_status}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=412, detail=str(e))

        audit.append(
            action="promote_asset",
            args={
                "name": name,
                "version": version,
                "to_status": body.to_status,
                "force": body.force,
            },
            cli_equivalent=(
                f"nova promote {name}@{version} --to {body.to_status}"
                + (" --force" if body.force else "")
            ),
            actor_token_fp=actor_fp,
            extra={"asset_id": result.get("id")},
        )
        return {"ok": True, "asset": _strip_spec(result)}

    # ---------- Layer B: Rollback ----------

    @app.post("/api/assets/{name}/rollback")
    async def rollback_asset_endpoint(
        name: str,
        body: RollbackRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        from novafabric.registry.service import (
            AssetNotFoundError,
            RollbackError,
            rollback_asset,
        )

        try:
            result = rollback_asset(
                name=name,
                target_version=None,
                actor=body.actor,
                db_path=db_path,
            )
        except AssetNotFoundError as e:
            audit.append(
                action="rollback_asset",
                args={"name": name, "reason": body.reason},
                cli_equivalent=f"nova rollback {name}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=404, detail=str(e))
        except RollbackError as e:
            audit.append(
                action="rollback_asset",
                args={"name": name, "reason": body.reason},
                cli_equivalent=f"nova rollback {name}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=409, detail=str(e))

        audit.append(
            action="rollback_asset",
            args={"name": name, "reason": body.reason},
            cli_equivalent=f"nova rollback {name}",
            actor_token_fp=actor_fp,
            extra={"result": result},
        )
        return {"ok": True, "result": result}

    # ---------- Layer B: Approvals ----------

    @app.post("/api/assets/{asset_id}/approve")
    async def approve_asset_endpoint(
        asset_id: str,
        body: ApproveRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        from novafabric.registry.service import (
            AssetNotFoundError,
            InvalidLifecycleTransitionError,
            approve_asset,
        )
        from novafabric.registry.store import get_connection, init_schema

        # Resolve UUID to name+version
        conn = get_connection(db_path)
        init_schema(conn)
        try:
            row = conn.execute(
                "SELECT name, version FROM assets WHERE id = ?", (asset_id,)
            ).fetchone()
        finally:
            conn.close()

        if not row:
            raise HTTPException(status_code=404, detail=f"Asset '{asset_id}' not found.")

        name, version = row["name"], row["version"]

        try:
            result = approve_asset(
                name=name,
                version=version,
                approver=body.actor,
                note=body.note,
                db_path=db_path,
            )
        except AssetNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except InvalidLifecycleTransitionError as e:
            raise HTTPException(status_code=409, detail=str(e))

        audit.append(
            action="approve_asset",
            args={"asset_id": asset_id, "role": body.role, "actor": body.actor},
            cli_equivalent=f"nova approve {name}@{version} --role {body.role}",
            actor_token_fp=actor_fp,
        )
        return {"ok": True, "asset_id": asset_id, "role": body.role, "result": result}

    # ---------- Layer B: Forensic replay ----------

    @app.post("/api/runs/{run_id}/replay/forensic")
    async def forensic_replay_endpoint(
        run_id: str,
        body: ForensicReplayRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            from novafabric.replay._engine import ReplayEngine
            from novafabric.replay._flags import ReplayFlags
        except ImportError as e:
            raise HTTPException(status_code=501, detail=f"replay module unavailable: {e}")

        flags = ReplayFlags(mode="forensic")
        engine = ReplayEngine(capsule_dir=cdir, flags=flags, base_dir=cdir.parent)
        try:
            result = engine.run()
        except Exception as e:  # noqa: BLE001
            audit.append(
                action="forensic_replay",
                args={"run_id": run_id},
                cli_equivalent=f"nova replay {cdir} --mode forensic",
                actor_token_fp=actor_fp,
                result="error",
                error=repr(e),
            )
            raise HTTPException(status_code=500, detail=f"forensic replay failed: {e}")

        if hasattr(result, "model_dump"):
            report = result.model_dump(mode="json")
        else:
            report = dict(result.__dict__)
        audit.append(
            action="forensic_replay",
            args={"run_id": run_id},
            cli_equivalent=f"nova replay {cdir} --mode forensic",
            actor_token_fp=actor_fp,
            extra={"replay_id": report.get("replay_id"), "status": report.get("status")},
        )
        return {"ok": True, "result": report}

    @app.post("/api/runs/{run_id}/replay/dry-run")
    async def dry_run_replay_endpoint(
        run_id: str,
        body: DryRunReplayRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            from novafabric.replay._engine import ReplayEngine
            from novafabric.replay._flags import ReplayFlags
        except ImportError as e:
            raise HTTPException(status_code=501, detail=f"replay module unavailable: {e}")

        flags = ReplayFlags(mode="forensic", dry_run=True)
        engine = ReplayEngine(capsule_dir=cdir, flags=flags, base_dir=cdir.parent)
        try:
            result = engine.run()
        except Exception as e:  # noqa: BLE001
            audit.append(
                action="dry_run_replay",
                args={"run_id": run_id},
                cli_equivalent=f"nova replay {cdir} --mode forensic --dry-run",
                actor_token_fp=actor_fp,
                result="error",
                error=repr(e),
            )
            raise HTTPException(status_code=500, detail=f"dry-run replay failed: {e}")

        if hasattr(result, "model_dump"):
            report = result.model_dump(mode="json")
        else:
            report = dict(result.__dict__)

        # Include the policy report text if written
        from novafabric.capture._ulid import new_ulid as _noop  # noqa: F401 (ensures path)
        report_path = cdir.parent / report.get("replay_id", "") / "dry_run_report.txt"
        if report_path.exists():
            report["dry_run_report"] = report_path.read_text()

        audit.append(
            action="dry_run_replay",
            args={"run_id": run_id},
            cli_equivalent=f"nova replay {cdir} --mode forensic --dry-run",
            actor_token_fp=actor_fp,
            extra={"replay_id": report.get("replay_id"), "status": report.get("status")},
        )
        return {"ok": True, "result": report}

    @app.post("/api/runs/{run_id}/replay/semantic")
    async def semantic_replay_endpoint(
        run_id: str,
        body: SemanticReplayRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            from novafabric.replay._engine import ReplayEngine
            from novafabric.replay._flags import ReplayFlags
        except ImportError as e:
            raise HTTPException(status_code=501, detail=f"replay module unavailable: {e}")

        flags = ReplayFlags(mode="semantic")
        engine = ReplayEngine(capsule_dir=cdir, flags=flags, base_dir=cdir.parent)
        try:
            result = engine.run()
        except Exception as e:  # noqa: BLE001
            audit.append(
                action="semantic_replay",
                args={"run_id": run_id},
                cli_equivalent=f"nova replay {cdir} --mode semantic",
                actor_token_fp=actor_fp,
                result="error",
                error=repr(e),
            )
            raise HTTPException(status_code=500, detail=f"semantic replay failed: {e}")

        if hasattr(result, "model_dump"):
            report = result.model_dump(mode="json")
        else:
            report = dict(result.__dict__)
        audit.append(
            action="semantic_replay",
            args={"run_id": run_id},
            cli_equivalent=f"nova replay {cdir} --mode semantic",
            actor_token_fp=actor_fp,
            extra={
                "replay_id": report.get("replay_id"),
                "similarity_score": report.get("similarity_score"),
            },
        )
        return {"ok": True, "result": report}

    @app.post("/api/runs/{run_id}/replay/exact")
    async def exact_replay_endpoint(
        run_id: str,
        body: ExactReplayRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            from novafabric.replay._engine import ReplayEngine
            from novafabric.replay._flags import ReplayFlags
        except ImportError as e:
            raise HTTPException(status_code=501, detail=f"replay module unavailable: {e}")

        flags = ReplayFlags(mode="exact")
        engine = ReplayEngine(capsule_dir=cdir, flags=flags, base_dir=cdir.parent)
        try:
            result = engine.run()
        except Exception as e:  # noqa: BLE001
            audit.append(
                action="exact_replay",
                args={"run_id": run_id},
                cli_equivalent=f"nova replay {cdir} --mode exact",
                actor_token_fp=actor_fp,
                result="error",
                error=repr(e),
            )
            raise HTTPException(status_code=500, detail=f"exact replay failed: {e}")

        if hasattr(result, "model_dump"):
            report = result.model_dump(mode="json")
        else:
            report = dict(result.__dict__)
        audit.append(
            action="exact_replay",
            args={"run_id": run_id},
            cli_equivalent=f"nova replay {cdir} --mode exact",
            actor_token_fp=actor_fp,
            extra={
                "replay_id": report.get("replay_id"),
                "exact_eligible": report.get("exact_eligible"),
            },
        )
        return {"ok": True, "result": report}

    # ---------- Layer B: Redact ----------

    @app.get("/api/runs/{run_id}/redaction-proof")
    async def get_redaction_proof_endpoint(
        run_id: str,
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        cdir = _resolve_capsule(run_id, capsule_dir)
        proof_path = cdir / "redaction-proof.json"
        if not proof_path.exists():
            raise HTTPException(
                status_code=404,
                detail="redaction proof not found — run `nova redact <capsule>` first",
            )
        try:
            import json as _json
            data: dict[str, Any] = _json.loads(proof_path.read_text())
            return data
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"failed to read proof: {e}")

    @app.post("/api/runs/{run_id}/redact")
    async def redact_endpoint(
        run_id: str,
        body: RedactRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="confirmation required (set confirmed=true)",
            )
        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            from novafabric.capture.secrets import SecretScannerV0
            from novafabric.cli.redact import _read_proof, _write_proof
        except ImportError as e:
            raise HTTPException(status_code=501, detail=f"redact module unavailable: {e}")

        proof_path = cdir / "redaction-proof.json"
        try:
            scanner = SecretScannerV0(capsule_dir=cdir, run_id=run_id, strategy_overrides=None)
            proof = scanner.scan_and_redact()
        except Exception as e:  # noqa: BLE001
            audit.append(
                action="redact",
                args={"run_id": run_id},
                cli_equivalent=f"nova redact {cdir}",
                actor_token_fp=actor_fp,
                result="error",
                error=repr(e),
            )
            raise HTTPException(status_code=500, detail=f"redact failed: {e}")

        # Preserve any prior unsafe_skips
        if proof_path.exists():
            try:
                prior = _read_proof(proof_path)
                if prior.get("unsafe_skips"):
                    proof["unsafe_skips"] = prior["unsafe_skips"]
            except Exception:  # noqa: BLE001
                pass
        _write_proof(proof_path, proof)

        findings = len(proof.get("findings", []))
        audit.append(
            action="redact",
            args={"run_id": run_id},
            cli_equivalent=f"nova redact {cdir}",
            actor_token_fp=actor_fp,
            extra={"findings_count": findings},
        )
        return {"ok": True, "findings_count": findings, "proof_path": str(proof_path)}

    # ---------- Layer B: Validate capsule ----------

    @app.post("/api/runs/{run_id}/validate", dependencies=[Depends(verify_token)])
    async def validate_capsule_endpoint(run_id: str) -> dict[str, Any]:
        """Validate a capsule's schema and required files.

        Replicates the logic of `nova validate <capsule_dir>` without
        calling the CLI function (which calls typer.Exit).  Returns
        ``{"valid": bool, "errors": list[str], "run_id": str}``.
        """
        import json as _json

        import jsonschema  # type: ignore[import-untyped]
        import yaml as _yaml

        cdir = _resolve_capsule(run_id, capsule_dir)

        _CAPSULE_SCHEMAS = {
            "capsule.yaml": "run-capsule.schema.json",
            "env.lock": "environment.schema.json",
            "redaction-proof.json": "secret-redaction.schema.json",
        }

        schema_dir = Path(__file__).parents[1] / "schemas"

        def _load_schema(name: str) -> dict[str, object]:
            return _json.loads((schema_dir / name).read_text())  # type: ignore[no-any-return]

        errors: list[str] = []

        # Validate schema-governed files
        for filename, schema_name in _CAPSULE_SCHEMAS.items():
            artifact = cdir / filename
            if not artifact.exists():
                errors.append(f"missing: {filename}")
                continue
            try:
                schema = _load_schema(schema_name)
                if filename.endswith(".json"):
                    data = _json.loads(artifact.read_text())
                else:
                    data = _yaml.safe_load(artifact.read_text())
                jsonschema.validate(data, schema, format_checker=jsonschema.FormatChecker())
            except jsonschema.ValidationError as exc:
                errors.append(f"{filename}: {exc.message}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{filename}: {exc}")

        # Required JSONL files
        for fname in ("trace.jsonl", "model-calls.jsonl", "tool-calls.jsonl", "assets.jsonl"):
            if not (cdir / fname).exists():
                errors.append(f"missing: {fname}")

        # Optional lineage.jsonl — validate if present
        lineage_path = cdir / "lineage.jsonl"
        if lineage_path.exists():
            lineage_schema = _load_schema("lineage-edge.schema.json")
            for i, line in enumerate(lineage_path.read_text().splitlines(), 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = _json.loads(line)
                    jsonschema.validate(record, lineage_schema,
                                       format_checker=jsonschema.FormatChecker())
                except _json.JSONDecodeError as exc:
                    errors.append(f"lineage.jsonl line {i}: invalid JSON: {exc}")
                except jsonschema.ValidationError as exc:
                    errors.append(f"lineage.jsonl line {i}: {exc.message}")

        # Required subdirectories
        for dname in ("inputs", "outputs"):
            if not (cdir / dname).is_dir():
                errors.append(f"missing directory: {dname}")

        return {"valid": len(errors) == 0, "errors": errors, "run_id": run_id}

    # ---------- Layer B: External score submission (ADR-0119, experimental) ----------

    @app.post("/api/runs/{run_id}/scores", status_code=201)
    async def submit_score_endpoint(
        run_id: str,
        body: dict = Body(...),  # type: ignore[type-arg]
        actor_fp: str = Depends(verify_token),
    ) -> JSONResponse:
        """Append one externally-computed score to the run's ``scores.jsonl``.

        The ADR-0119 ingest surface over the shared validation core
        (``novafabric.eval.score_submission``): fail-closed (a rejection writes
        nothing), append-only (corrections are new ``supersedes`` records, never
        edits), idempotent by ``score_id`` (identical replay → 200, no second
        line). Mirrors ``nova score submit``.
        """
        from datetime import datetime, timezone  # noqa: PLC0415

        from pydantic import ValidationError as _PydanticValidationError  # noqa: PLC0415

        from novafabric.eval.score_config import ScoreConfigViolation  # noqa: PLC0415
        from novafabric.eval.score_submission import (  # noqa: PLC0415
            CapsuleNotFoundError,
            IdempotencyConflictError,
            ScoreSubmissionRequest,
            SubjectNotFoundError,
            SubmissionInvalidError,
            SupersedesNotFoundError,
            submit_request,
        )

        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            request = ScoreSubmissionRequest.model_validate(body)
        except _PydanticValidationError as exc:
            raise HTTPException(status_code=400, detail=f"malformed score submission: {exc}")

        def _audit(result: str, error: str | None = None, **extra: Any) -> None:
            audit.append(
                action="score_submit",
                args={"run_id": run_id, "name": request.name,
                      "evaluator_id": request.evaluator_id},
                cli_equivalent=f"nova score submit --capsule {cdir} --name {request.name}",
                actor_token_fp=actor_fp,
                result=result,
                error=error,
                extra=extra or None,
            )

        try:
            result = submit_request(cdir, request, db_path=db_path)
        except SubmissionInvalidError as exc:
            _audit("error", error=str(exc))
            raise HTTPException(status_code=400, detail=str(exc))
        except (CapsuleNotFoundError, SubjectNotFoundError) as exc:
            _audit("error", error=str(exc))
            raise HTTPException(status_code=404, detail=str(exc))
        except IdempotencyConflictError as exc:
            _audit("error", error=str(exc))
            raise HTTPException(status_code=409, detail=str(exc))
        except (SupersedesNotFoundError, ScoreConfigViolation) as exc:
            _audit("error", error=str(exc))
            raise HTTPException(status_code=422, detail=str(exc))

        _audit("ok", score_id=result.score.score_id,
               idempotent_replay=result.idempotent_replay)
        return JSONResponse(
            status_code=200 if result.idempotent_replay else 201,
            content={
                "score": json.loads(result.score.model_dump_json(exclude_none=True)),
                "idempotent_replay": result.idempotent_replay,
                "config_bound": result.config_bound,
                "submission": {
                    "principal": f"token:{actor_fp}",
                    "scope": "scores:write",
                    "received_at": datetime.now(timezone.utc).isoformat(),
                },
            },
        )

    # ---------- Policy check (DC-5) ----------

    @app.post("/api/policy/check", dependencies=[Depends(verify_token)])
    async def policy_check_endpoint(
        body: dict = Body(...),  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """Evaluate a policy check interactively.

        Body: PolicyInput JSON
        Response: PolicyDecision JSON
        """
        import uuid as _uuid

        from novafabric.policy import OpaEngine, OpaNotFoundError, PolicyInput

        explain = bool(body.pop("explain", False))
        policy_source: str | None = body.pop("policy_source", None) or None

        try:
            policy_input = PolicyInput.model_validate(body)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"invalid policy input: {exc}")

        try:
            engine = OpaEngine()
            decision = engine.evaluate(policy_input, explain=explain, policy_source=policy_source)
        except OpaNotFoundError:
            from novafabric.policy import PolicyDecision
            decision = PolicyDecision(
                allow=False,
                reason="opa binary not found — install OPA to evaluate policies",
                decision_id=str(_uuid.uuid4()),
                policy_path="",
            )

        return decision.model_dump(mode="json", exclude={"input_snapshot"})

    # ---------- DD-7: Object Capsule Store browser ----------

    @app.get("/api/storage/stats", dependencies=[Depends(verify_token)])
    async def storage_stats_endpoint() -> dict[str, Any]:
        """Return object capsule store statistics."""
        backend_type = os.environ.get("NOVA_OBJECT_STORE_BACKEND", "local-wal")
        configured = (
            os.environ.get("NOVA_OBJECT_STORE_PATH") is not None
            or os.environ.get("NOVA_S3_BUCKET") is not None
        )
        if not configured:
            return {"configured": False, "backend_type": backend_type}

        try:
            stats: dict[str, Any] = {
                "configured": True,
                "backend_type": backend_type,
                "total_chunks": None,
                "total_size_bytes": None,
                "worm_score": None,
                "manifest_chain_head": None,
                "last_put_p99_ms": None,
            }
            wal_path = Path.home() / ".novafabric" / "object_store"
            if wal_path.exists():
                chunk_files = list(wal_path.glob("*.chunk"))
                stats["total_chunks"] = len(chunk_files)
                stats["total_size_bytes"] = sum(f.stat().st_size for f in chunk_files)
            return stats
        except Exception:  # noqa: BLE001
            return {
                "configured": True,
                "backend_type": backend_type,
                "error": "could not load stats",
            }

    @app.get("/api/storage/manifest-chain", dependencies=[Depends(verify_token)])
    async def manifest_chain_endpoint(
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict[str, Any]:
        """Return the last N entries in the manifest chain."""
        import json as _json

        wal_path = Path.home() / ".novafabric" / "object_store"
        if not wal_path.exists():
            return {"configured": False, "entries": []}

        try:
            manifest_files = sorted(
                wal_path.glob("manifest_*.jsonl"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            entries: list[dict[str, Any]] = []
            for mf in manifest_files[:limit]:
                try:
                    line = mf.read_text().strip().splitlines()[-1] if mf.stat().st_size > 0 else ""
                    if line:
                        record = _json.loads(line)
                        entries.append({
                            "hash": record.get("hash", mf.stem)[:12],
                            "run_id": record.get("run_id", ""),
                            "timestamp": record.get("timestamp", ""),
                            "size_bytes": record.get("size_bytes", mf.stat().st_size),
                        })
                except Exception:  # noqa: BLE001
                    pass
            return {"configured": True, "entries": entries}
        except Exception:  # noqa: BLE001
            return {"configured": True, "entries": [], "error": "could not read manifest chain"}

    # ---------- DD-2: Collector status ----------

    @app.get("/api/infra/collector", dependencies=[Depends(verify_token)])
    async def collector_status_endpoint() -> dict[str, Any]:
        """Return collector health. Returns {detected: false} if not running."""
        import json as _json

        _default_health = "/tmp/novafabric-collector-health.json"  # noqa: S108
        health_paths = [
            Path(_default_health),
            Path.home() / ".novafabric" / "collector-health.json",
            Path(os.environ.get("NOVA_COLLECTOR_HEALTH_FILE", _default_health)),
        ]

        for health_path in health_paths:
            if health_path.exists():
                try:
                    data = _json.loads(health_path.read_text())
                    return {
                        "detected": True,
                        "spool_lag": data.get("spool_lag", 0),
                        "signing_p99_ms": data.get("signing_p99_ms"),
                        "events_per_sec": data.get("events_per_sec"),
                        "last_heartbeat": data.get("last_heartbeat"),
                        "collector_version": data.get("version", "unknown"),
                        "source": str(health_path),
                    }
                except Exception:  # noqa: BLE001
                    pass

        try:
            import urllib.request

            collector_url = os.environ.get(
                "NOVA_COLLECTOR_METRICS_URL", "http://localhost:9464/metrics"
            )
            with urllib.request.urlopen(collector_url, timeout=1) as resp:  # noqa: S310
                raw = resp.read().decode()
            spool_lag = _parse_prom_gauge(raw, "nova_spool_queue_depth")
            events_sec = _parse_prom_gauge(raw, "nova_events_processed_total")
            return {
                "detected": True,
                "spool_lag": spool_lag,
                "signing_p99_ms": None,
                "events_per_sec": events_sec,
                "last_heartbeat": None,
                "collector_version": "running",
                "source": collector_url,
            }
        except Exception:  # noqa: BLE001
            pass

        return {"detected": False}

    # ---------- Admin: token + role management (DD-8) ----------

    @app.get("/api/admin/tokens", dependencies=[Depends(verify_token)])
    async def list_tokens_endpoint() -> dict[str, Any]:
        """List issued local tokens (stored in ~/.novafabric/tokens.jsonl)."""
        import json as _json

        tokens_path = Path.home() / ".novafabric" / "tokens.jsonl"
        tokens: list[dict[str, Any]] = []
        if tokens_path.exists():
            for line in tokens_path.read_text().splitlines():
                if line.strip():
                    try:
                        t = _json.loads(line)
                        # Never return the raw token value — only fingerprint + metadata
                        tokens.append({
                            "label": t.get("label", ""),
                            "fingerprint": t.get("fingerprint", ""),
                            "created_at": t.get("created_at", ""),
                            "revoked": t.get("revoked", False),
                        })
                    except Exception:  # noqa: BLE001
                        pass
        return {"tokens": tokens, "session_token_fingerprint": token[:8]}

    @app.post("/api/admin/tokens")
    async def issue_token_endpoint(
        body: IssueTokenRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Issue a new local session token."""
        import hashlib
        import json as _json
        import secrets as _secrets
        from datetime import datetime, timezone

        if not body.confirmed:
            raise HTTPException(
                status_code=400, detail="confirmation required (set confirmed=true)"
            )
        new_token = _secrets.token_urlsafe(32)
        fp = hashlib.sha256(new_token.encode()).hexdigest()[:16]
        record = {
            "label": body.label,
            "token": new_token,  # stored locally only, never transmitted again
            "fingerprint": fp,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "revoked": False,
        }
        tokens_path = Path.home() / ".novafabric" / "tokens.jsonl"
        tokens_path.parent.mkdir(parents=True, exist_ok=True)
        with tokens_path.open("a") as f:
            f.write(_json.dumps(record) + "\n")
        audit.append(
            action="issue_token",
            args={"label": body.label},
            cli_equivalent=f"nova server issue-token --label {body.label}",
            actor_token_fp=actor_fp,
            extra={"fingerprint": fp},
        )
        # Return the token value ONCE — after this it's only available via the file
        return {
            "ok": True,
            "token": new_token,
            "fingerprint": fp,
            "label": body.label,
            "warning": "Save this token — it will not be shown again.",
        }

    @app.delete("/api/admin/tokens/{fingerprint}")
    async def revoke_token_endpoint(
        fingerprint: str,
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Revoke a token by fingerprint."""
        import json as _json
        import os as _os

        if "/" in fingerprint or ".." in fingerprint:
            raise HTTPException(status_code=400, detail="invalid fingerprint")
        tokens_path = Path.home() / ".novafabric" / "tokens.jsonl"
        if not tokens_path.exists():
            raise HTTPException(status_code=404, detail="token not found")
        lines = tokens_path.read_text().splitlines()
        updated = []
        found = False
        for line in lines:
            if not line.strip():
                continue
            t = _json.loads(line)
            if t.get("fingerprint") == fingerprint:
                t["revoked"] = True
                found = True
            updated.append(_json.dumps(t))
        if not found:
            raise HTTPException(status_code=404, detail="token not found")
        tmp = tokens_path.with_suffix(".tmp")
        tmp.write_text("\n".join(updated) + "\n")
        _os.replace(tmp, tokens_path)
        audit.append(
            action="revoke_token",
            args={"fingerprint": fingerprint},
            cli_equivalent=f"nova server revoke-token {fingerprint}",
            actor_token_fp=actor_fp,
        )
        return {"ok": True, "fingerprint": fingerprint, "revoked": True}

    @app.get("/api/admin/roles", dependencies=[Depends(verify_token)])
    async def list_roles_endpoint(
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """List role assignments (local mode).

        ADR-0060: local experimental mode exposes the same role surface as
        production /v0/admin/roles, sharing the rbac_store backend. The shared
        token in serve mode is implicitly admin.
        """
        from novafabric.server import rbac_store

        oidc_configured = bool(
            os.environ.get("NOVA_OIDC_ISSUER") or os.environ.get("NOVA_OIDC_CLIENT_ID")
        )
        assignments = rbac_store.list_assignments()
        enriched = [
            {**row, "effective_now": not oidc_configured} for row in assignments
        ]
        return {
            "server_mode": oidc_configured,
            "roles": enriched,
            "message": (
                ""
                if oidc_configured
                else (
                    "Role management requires server mode with OIDC configured"
                    " (NOVA_OIDC_ISSUER, NOVA_OIDC_CLIENT_ID) for assignments to"
                    " affect live authorization. In local mode assignments are"
                    " stored but the shared dashboard token is unconditionally admin."
                )
            ),
        }

    @app.post("/api/admin/roles", status_code=201)
    async def assign_role_endpoint(
        body: AssignRoleRequest,
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Assign a role to a subject (idempotent). Local-mode admin shortcut."""
        from novafabric.server import rbac_store
        from novafabric.server.rbac import Role

        try:
            role_enum = Role(body.role)
        except ValueError as e:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"invalid role {body.role!r}; "
                    f"must be one of {[r.value for r in Role]}"
                ),
            ) from e

        rbac_store.assign_role(body.subject, role_enum.value, f"local:{actor_fp}")
        audit.append(
            action="assign_role",
            args={"subject": body.subject, "role": role_enum.value},
            cli_equivalent=f"nova server assign-role {body.subject} {role_enum.value}",
            actor_token_fp=actor_fp,
            result="ok",
        )
        return {
            "ok": True,
            "subject": body.subject,
            "role": role_enum.value,
            "assigned_by": f"local:{actor_fp}",
        }

    @app.delete("/api/admin/roles/{subject}/{role}")
    async def revoke_role_endpoint(
        subject: str,
        role: str,
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Revoke a role from a subject. 404 if not found, 409 if lockout would occur."""
        from novafabric.server import rbac_store
        from novafabric.server.rbac_store import LastAdminError

        try:
            deleted = rbac_store.revoke_role(subject, role)
        except LastAdminError as e:
            audit.append(
                action="revoke_role",
                args={"subject": subject, "role": role},
                cli_equivalent=f"nova server revoke-role {subject} {role}",
                actor_token_fp=actor_fp,
                result="error",
                error=str(e),
            )
            raise HTTPException(status_code=409, detail=str(e)) from e

        if not deleted:
            audit.append(
                action="revoke_role",
                args={"subject": subject, "role": role},
                cli_equivalent=f"nova server revoke-role {subject} {role}",
                actor_token_fp=actor_fp,
                result="error",
                error="not found",
            )
            raise HTTPException(
                status_code=404,
                detail=f"no assignment of role {role!r} to {subject!r}",
            )

        audit.append(
            action="revoke_role",
            args={"subject": subject, "role": role},
            cli_equivalent=f"nova server revoke-role {subject} {role}",
            actor_token_fp=actor_fp,
            result="ok",
        )
        return {"ok": True, "subject": subject, "role": role}

    # ---------- Tool permission events (ComplianceTab / cap-004) ----------

    def _tool_permission_db_path() -> Path:
        env = os.environ.get("NOVAFABRIC_TOOL_PERMISSION_DB_PATH")
        if env:
            return Path(env)
        nova_data = Path(os.environ.get("NOVAFABRIC_HOME", str(Path.home() / ".novafabric")))
        return nova_data / "compliance" / "tool_permission_idx.db"

    @app.get("/api/runs/{run_id}/tool-permission-events", dependencies=[Depends(verify_token)])
    async def tool_permission_events_endpoint(run_id: str) -> dict[str, Any]:
        """Return ToolPermissionEvent records for a capsule."""
        from novafabric.compliance.tool_permission.index import PermissionEventIndex

        db_path = _tool_permission_db_path()
        if not db_path.exists():
            return {"run_id": run_id, "events": []}
        idx = PermissionEventIndex(db_path)
        try:
            idx.open()
            events = idx.query_by_capsule(run_id)
        finally:
            idx.close()
        return {"run_id": run_id, "events": [e.model_dump() for e in events]}

    # ---------- Compliance exports (ComplianceTab — ADR-0054/0055/0057) ----------

    @app.get("/api/compliance/annex-iv", dependencies=[Depends(verify_token)])
    async def compliance_annex_iv_endpoint(
        run_id: str,
        deployment_id: str,
    ) -> dict[str, Any]:
        """Build and return an EU AI Act Annex IV document as JSON-LD."""
        try:
            from novafabric.compliance.export.annex_iv import AnnexIVExporter
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"compliance module not available: {exc}",
            ) from exc

        capsule_path = _resolve_capsule(run_id, capsule_dir)
        try:
            exporter = AnnexIVExporter()
            document = exporter.build_annex_iv_document(
                deployment_id=deployment_id,
                capsule_dir=capsule_path,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"Annex IV export failed: {exc}") from exc

        complete_count = sum(1 for e in document.elements if e.completeness_flag == "complete")
        return {
            "run_id": run_id,
            "deployment_id": deployment_id,
            "complete_elements": complete_count,
            "total_elements": len(document.elements),
            "document": document.model_dump(),
        }

    @app.get("/api/compliance/nis2", dependencies=[Depends(verify_token)])
    async def compliance_nis2_endpoint(
        run_id: str,
        incident_id: str,
        phase: int = 1,
    ) -> dict[str, Any]:
        """Build and return a NIS2 incident report as JSON."""
        try:
            from novafabric.compliance.export.nis2 import NIS2Exporter
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"compliance module not available: {exc}",
            ) from exc

        if phase not in (1, 2, 3):
            raise HTTPException(status_code=422, detail="phase must be 1, 2, or 3")

        capsule_path = _resolve_capsule(run_id, capsule_dir)
        try:
            exporter = NIS2Exporter(cap006_available=False)
            report = exporter.build_nis2_report(
                incident_id=incident_id,
                capsule_dir=capsule_path,
                phase=phase,
            )
        except Exception as exc:
            raise HTTPException(status_code=422, detail=f"NIS2 export failed: {exc}") from exc

        missing_count = sum(1 for e in report.completeness_summary if e.status == "missing")
        return {
            "run_id": run_id,
            "incident_id": incident_id,
            "phase": phase,
            "missing_fields": missing_count,
            "report": report.model_dump(),
        }

    @app.get("/api/compliance/subject-proof", dependencies=[Depends(verify_token)])
    async def compliance_subject_proof_endpoint(subject_id: str) -> dict[str, Any]:
        """Return GDPR Art. 17 redaction proof for a data subject."""
        import hashlib
        import hmac
        from datetime import datetime, timezone

        try:
            from novafabric.compliance.pii.index import RedactionSubjectIndex
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"compliance module not available: {exc}",
            ) from exc

        pepper_raw = os.environ.get("NOVA_PII_PEPPER", "")
        if not pepper_raw:
            raise HTTPException(
                status_code=503,
                detail="NOVA_PII_PEPPER env var not set — required for subject proof lookup",
            )
        pepper = pepper_raw.encode("utf-8")
        mac = hmac.new(pepper, subject_id.encode("utf-8"), hashlib.sha256)
        subject_hmac = "sha256:" + mac.hexdigest()

        nova_data = Path(os.environ.get("NOVAFABRIC_HOME", str(Path.home() / ".novafabric")))
        db_path = nova_data / "compliance" / "redaction_subject_idx.db"

        if not db_path.exists():
            return {
                "subject_id_hmac": subject_hmac,
                "records": [],
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "note": "Redaction index not found — no records for this subject.",
            }

        idx = RedactionSubjectIndex(db_path=db_path)
        try:
            with idx:
                records = idx.lookup_subject(subject_hmac)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Subject proof lookup failed: {exc}",
            ) from exc

        return {
            "subject_id_hmac": subject_hmac,
            "records": [
                {
                    "capsule_id": r.capsule_id,
                    "field_path": r.field_path,
                    "legal_basis": r.legal_basis,
                    "redacted_at_utc": r.redacted_at_utc,
                }
                for r in records
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ---------- Governance: risk classification (GovernanceTab — ADR-0056) ----------

    @app.get("/api/governance/classify", dependencies=[Depends(verify_token)])
    async def governance_classify_endpoint(
        run_id: str,
        vocabulary: str = "eu-ai-act/2024.1.0",
    ) -> dict[str, Any]:
        """Classify an AI system risk tier inferred from a Run Capsule."""
        try:
            from novafabric.governance.classifier import RiskTierClassifier
            from novafabric.governance.models import AISystemRecord
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"governance module not available: {exc}",
            ) from exc

        valid_vocabs = {"eu-ai-act/2024.1.0", "nist-ai-rmf/1.0.0", "omb-m-24-10/1.0.0"}
        if vocabulary not in valid_vocabs:
            raise HTTPException(
                status_code=422,
                detail=f"unknown vocabulary {vocabulary!r}; valid: {sorted(valid_vocabs)}",
            )

        capsule_path = _resolve_capsule(run_id, capsule_dir)
        try:
            import yaml as _yaml
            manifest_path = capsule_path / "capsule.yaml"
            manifest: dict[str, Any] = {}
            if manifest_path.exists():
                raw = _yaml.safe_load(manifest_path.read_text())
                if isinstance(raw, dict):
                    manifest = raw
        except Exception:
            manifest = {}

        # Build a minimal AISystemRecord from capsule metadata.
        # All fields default to safe/general-purpose values so the classifier
        # can always return a result; operators refine via the CLI for detailed reports.
        cmd_parts = manifest.get("command", []) or []
        cmd_snippet = " ".join(str(p) for p in cmd_parts[:3]) if cmd_parts else "captured run"
        asset_name = manifest.get("asset_name") or manifest.get("run_id", run_id)

        record = AISystemRecord(
            name=str(asset_name)[:60],
            description=f"Captured run: {cmd_snippet}",
            use_case_domain="general",
            deployment_context="captured_run",
            is_general_purpose=True,
        )

        try:
            classifier = RiskTierClassifier()
            result = classifier.classify(record)
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"classification failed: {exc}",
            ) from exc

        return {
            "run_id": run_id,
            "vocabulary": vocabulary,
            "result": result.model_dump(),
            "note": (
                "Classification uses minimal capsule metadata. "
                "For accurate results, run `nova classify from-capsule <path> --vocabulary "
                + vocabulary + "` with a fully-populated system description."
            ),
        }

    # ---------- Compliance audit (ComplianceTab — ADR-0061) ----------

    @app.get("/api/compliance/audit/map", dependencies=[Depends(verify_token)])
    async def compliance_audit_map_endpoint(
        profile: str = "nist-ai-rmf",
    ) -> dict[str, Any]:
        """List evidence checkers for a compliance profile."""
        try:
            from novafabric.compliance.audit.loader import load_profile
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"compliance audit module not available: {exc}",
            ) from exc

        try:
            ctrl_profile = load_profile(profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        checkers: list[dict[str, Any]] = []
        for ctrl in ctrl_profile.controls:
            for req in ctrl.evidence_requirements:
                checkers.append({
                    "control_id": ctrl.id,
                    "control_title": ctrl.title,
                    "evidence_type": req.evidence_type,
                    "required": req.required,
                    "description": req.description,
                })

        return {
            "profile": profile,
            "profile_name": ctrl_profile.name,
            "framework": ctrl_profile.framework,
            "control_count": len(ctrl_profile.controls),
            "checkers": checkers,
        }

    @app.post("/api/compliance/audit/report", dependencies=[Depends(verify_token)])
    async def compliance_audit_report_endpoint(body: dict[str, Any]) -> dict[str, Any]:
        """Run compliance audit against a capsule and return a coverage report."""
        try:
            from novafabric.compliance.audit.engine import AuditEngine
            from novafabric.compliance.audit.loader import load_profile
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"compliance audit module not available: {exc}",
            ) from exc

        run_id = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        profile = body.get("profile", "nist-ai-rmf")

        capsule_path = _resolve_capsule(run_id, capsule_dir)

        try:
            ctrl_profile = load_profile(profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        # AuditEngine scans capsules/ under data_dir; for a single-capsule audit
        # we pass the parent dir as data_dir and filter by the specific capsule.
        data_dir = capsule_path.parent
        try:
            engine = AuditEngine(data_dir=data_dir, profile=ctrl_profile)
            report = engine.scan()
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"audit scan failed: {exc}",
            ) from exc

        return {
            "run_id": run_id,
            "profile": profile,
            **report.model_dump(by_alias=True),
        }

    # ---------- Examiner exports (ComplianceTab — ADR-0061/0062) ----------

    @app.post("/api/compliance/examiner/{format}", dependencies=[Depends(verify_token)])
    async def compliance_examiner_endpoint(
        format: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Export a capsule in examiner format (bagit | pccp | iso42001)."""
        valid_formats = {"bagit", "pccp", "iso42001"}
        if format not in valid_formats:
            raise HTTPException(
                status_code=422,
                detail=f"unknown examiner format {format!r}; valid: {sorted(valid_formats)}",
            )

        try:
            from novafabric.compliance.export.examiner import (
                BagItExporter,
                ISO42001Exporter,
                PCCPExporter,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"examiner module not available: {exc}",
            ) from exc

        run_id = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")

        capsule_path = _resolve_capsule(run_id, capsule_dir)
        import tempfile

        try:
            if format == "bagit":
                with tempfile.TemporaryDirectory() as tmp:
                    bagit_exp = BagItExporter(capsule_id=run_id, data_dir=capsule_path)
                    out = bagit_exp.export(output_dir=Path(tmp))
                    size = out.stat().st_size
                return {
                    "ok": True,
                    "format": "bagit",
                    "run_id": run_id,
                    "output_path": f"{run_id}-bag.zip",
                    "size_bytes": size,
                    "note": "BagIt ZIP generated in-memory. Use "
                            "`nova export-examiner bagit` to write to disk.",
                }

            elif format == "pccp":
                # PCCP requires baseline + proposed capsule IDs; for dashboard
                # use the same capsule as both, indicating single-capsule report.
                baseline = body.get("baseline_run_id", run_id)
                with tempfile.TemporaryDirectory() as tmp:
                    pccp_exp = PCCPExporter(
                        baseline_capsule_id=baseline,
                        proposed_capsule_id=run_id,
                        data_dir=capsule_path.parent,
                    )
                    out = pccp_exp.export(output_path=Path(tmp) / "pccp.json")
                    size = out.stat().st_size
                return {
                    "ok": True,
                    "format": "pccp",
                    "run_id": run_id,
                    "output_path": "pccp.json",
                    "size_bytes": size,
                    "note": "PCCP document generated. "
                            "Use `nova export-examiner pccp` to write to disk.",
                }

            else:  # iso42001
                from datetime import datetime, timezone

                period_start = datetime(2026, 1, 1, tzinfo=timezone.utc)
                period_end = datetime.now(timezone.utc)
                with tempfile.TemporaryDirectory() as tmp:
                    iso_exp = ISO42001Exporter(
                        data_dir=capsule_path.parent,
                        period_start=period_start,
                        period_end=period_end,
                    )
                    out = iso_exp.export(output_path=Path(tmp) / "iso42001.zip")
                    size = out.stat().st_size
                return {
                    "ok": True,
                    "format": "iso42001",
                    "run_id": run_id,
                    "output_path": "iso42001.zip",
                    "size_bytes": size,
                    "note": "ISO 42001 evidence package generated. "
                            "Use `nova export-examiner iso42001` to write to disk.",
                }

        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500,
                detail=f"examiner export failed: {exc}",
            ) from exc

    # ---------- NovaSeal maker-checker (SealTab) ----------

    def _seal_db_path() -> Path:
        from novafabric.trust.novaseal.config import resolve_merkle_db_path
        return resolve_merkle_db_path()

    @app.get("/api/seal/policy", dependencies=[Depends(verify_token)])
    async def seal_policy_endpoint() -> dict[str, Any]:
        """Return the latest promotion policy predicate.

        When no policy has been signed yet, this returns 200 with
        ``configured: false`` (rather than 404) so the read-only dashboard can
        render a clean "no policy yet" empty state without logging a browser
        console error on every load. ``nova policy sign`` creates one.
        """
        import base64
        import json as _json
        import sqlite3

        from novafabric.promote.exceptions import PolicyNotFoundError
        from novafabric.promote.policy_store import PolicyStore

        db_path = _seal_db_path()
        policy_store = PolicyStore(db_path)
        try:
            try:
                version = policy_store.get_latest_version()
                bundle_json = policy_store.get_latest()
            except PolicyNotFoundError:
                return {
                    "configured": False,
                    "version": None,
                    "predicate": None,
                    "created_at": None,
                    "detail": "No promotion policy — run `nova policy sign` to create one.",
                }
        finally:
            policy_store.close()

        bundle = _json.loads(bundle_json)
        if "payloadType" in bundle and "payload" in bundle:
            raw = bundle["payload"]
            pad = 4 - len(raw) % 4
            if pad != 4:
                raw += "=" * pad
            predicate: dict[str, Any] = _json.loads(base64.urlsafe_b64decode(raw))
        else:
            predicate = bundle

        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT created_at FROM promote_policy "
                "WHERE namespace = 'default' ORDER BY version DESC LIMIT 1"
            ).fetchone()
            created_at = str(row[0]) if row else ""
        finally:
            conn.close()

        return {
            "configured": True,
            "version": version,
            "predicate": predicate,
            "created_at": created_at,
        }

    @app.get("/api/seal/{capsule_id}/proposals", dependencies=[Depends(verify_token)])
    async def seal_list_proposals_endpoint(capsule_id: str) -> list[dict[str, Any]]:
        """List all proposals for a capsule with their approval status."""
        import json as _json

        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.exceptions import BundleNotFoundError
        from novafabric.promote.predicates import (
            APPROVAL_PAYLOAD_TYPE,
            PROPOSAL_PAYLOAD_TYPE,
            EnvelopeError,
            verify_promote_envelope,
        )

        nova_data = Path(os.environ.get("NOVAFABRIC_HOME", str(Path.home() / ".novafabric")))
        bundle_store = PromoteBundleStore(nova_data)
        summaries: list[dict[str, Any]] = []

        for proposal_uuid in bundle_store.list_proposals(capsule_id):
            proposer_subject = justification = timestamp = policy_version = ""
            has_approval = False
            approver_subject: str | None = None
            approval_timestamp: str | None = None

            try:
                payload_bytes, ps = verify_promote_envelope(
                    bundle_store.get_proposal(capsule_id, proposal_uuid), PROPOSAL_PAYLOAD_TYPE
                )
                proposer_subject = ps
                pred = _json.loads(payload_bytes)
                justification = pred.get("justification", "")
                timestamp = pred.get("timestamp", "")
                policy_version = str(pred.get("policy_version", ""))
            except (BundleNotFoundError, EnvelopeError, ValueError):  # pragma: no cover
                pass

            try:
                apayload_bytes, as_ = verify_promote_envelope(
                    bundle_store.get_approval(capsule_id, proposal_uuid), APPROVAL_PAYLOAD_TYPE
                )
                has_approval = True
                approver_subject = as_
                approval_timestamp = _json.loads(apayload_bytes).get("timestamp")
            except (BundleNotFoundError, EnvelopeError, ValueError):
                pass

            summaries.append({
                "uuid": proposal_uuid,
                "capsule_id": capsule_id,
                "proposer_subject": proposer_subject,
                "justification": justification,
                "timestamp": timestamp,
                "policy_version": policy_version,
                "has_approval": has_approval,
                "approver_subject": approver_subject,
                "approval_timestamp": approval_timestamp,
            })

        return summaries

    _SEAL_CHECK_NAMES = [
        "Proposal signature + proposer in policy",
        "Approval signature + approver in policy",
        "proposal_digest match",
        "No self-approval",
        "Timestamp ordering (approval > proposal)",
    ]
    _SEAL_EXIT_TO_CHECK: dict[int, int] = {3: 0, 4: 1, 5: 2, 6: 3, 7: 4}

    # ---------- v0.44.0: Sigstore keyless signing / verification routes ----------
    # IMPORTANT: these static-path routes MUST be registered before the parametric
    # /api/seal/{capsule_id}/verify route so FastAPI routes them correctly.

    @app.post("/api/seal/sigstore/sign", dependencies=[Depends(verify_token)])
    async def seal_sigstore_sign_endpoint(body: dict[str, Any]) -> dict[str, Any]:
        """Sign a capsule artifact using Sigstore keyless signing (ADR-0071).

        Requires novafabric[sigstore] to be installed.  Uses the ambient OIDC
        provider (GitHub Actions, Google Workload Identity, interactive browser
        flow) to obtain a Fulcio certificate and Rekor inclusion proof.
        The resulting Sigstore bundle v0.3 is persisted to disk alongside
        capsule metadata.
        """
        try:
            from novafabric.trust.novaseal.sigstore_signer import (  # noqa: PLC0415
                SigstoreBundleStore,
                SigstoreSigner,
            )
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail=(
                    "novafabric[sigstore] not installed; "
                    "run: pip install novafabric[sigstore]"
                ),
            )

        capsule_id = body.get("capsule_id", "").strip()
        if not capsule_id:
            raise HTTPException(status_code=422, detail="capsule_id is required")

        manifest_json = body.get("manifest_json") or f'{{"capsule_id": "{capsule_id}"}}'
        artifact_bytes = (
            manifest_json.encode("utf-8") if isinstance(manifest_json, str) else manifest_json
        )

        home = Path(os.environ.get("NOVAFABRIC_HOME", str(Path.home() / ".novafabric")))
        signer = SigstoreSigner()
        try:
            bundle = signer.sign_artifact(artifact_bytes)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc))

        bundle_path = SigstoreBundleStore.store_bundle(capsule_id, bundle, home)
        log_index: int | None = None
        try:
            tlog_entries = bundle.get("verificationMaterial", {}).get("tlogEntries", [])
            if tlog_entries:
                raw_idx = tlog_entries[0].get("logIndex")
                if raw_idx is not None:
                    log_index = int(raw_idx)
        except Exception:
            pass

        identity: str | None = None
        try:
            cert_b64 = (
                bundle.get("verificationMaterial", {})
                .get("certificate", {})
                .get("rawBytes")
            )
            if cert_b64:
                import base64  # noqa: PLC0415

                from cryptography.x509 import load_der_x509_certificate  # noqa: PLC0415

                cert = load_der_x509_certificate(base64.b64decode(cert_b64))
                for ext in cert.extensions:
                    val = ext.value
                    if hasattr(val, "value") and "@" in str(val.value):
                        identity = str(val.value)
                        break
        except Exception:
            pass

        return {
            "ok": True,
            "capsule_id": capsule_id,
            "bundle_path": str(bundle_path),
            "log_index": log_index,
            "identity": identity,
            "note": (
                "Sigstore Bundle v0.3 stored; "
                "verify with nova verify --backend sigstore"
            ),
        }

    @app.post("/api/seal/sigstore/verify", dependencies=[Depends(verify_token)])
    async def seal_sigstore_verify_endpoint(body: dict[str, Any]) -> dict[str, Any]:
        """Verify a stored Sigstore bundle for a capsule (ADR-0071).

        Loads the bundle written by ``/api/seal/sigstore/sign`` and runs
        Sigstore's offline verification against the stored inclusion proof.
        Requires novafabric[sigstore] to be installed.
        """
        try:
            from novafabric.trust.novaseal.sigstore_signer import (  # noqa: PLC0415
                SigstoreBundleStore,
                SigstoreSigner,
            )
        except ImportError:
            raise HTTPException(
                status_code=501,
                detail=(
                    "novafabric[sigstore] not installed; "
                    "run: pip install novafabric[sigstore]"
                ),
            )

        capsule_id = body.get("capsule_id", "").strip()
        if not capsule_id:
            raise HTTPException(status_code=422, detail="capsule_id is required")

        home = Path(os.environ.get("NOVAFABRIC_HOME", str(Path.home() / ".novafabric")))
        bundle = SigstoreBundleStore.load_bundle(capsule_id, home)
        if bundle is None:
            return {
                "ok": False,
                "capsule_id": capsule_id,
                "valid": False,
                "identity": None,
                "rekor_log_index": None,
                "error": f"No Sigstore bundle found for capsule_id={capsule_id!r}",
            }

        # Re-derive the artifact bytes from the capsule_id (same as sign path)
        manifest_json = f'{{"capsule_id": "{capsule_id}"}}'
        artifact_bytes = manifest_json.encode("utf-8")

        signer = SigstoreSigner()
        result = signer.verify_bundle(bundle, artifact_bytes)

        return {
            "ok": True,
            "capsule_id": capsule_id,
            "valid": result.valid,
            "identity": result.identity,
            "rekor_log_index": result.rekor_log_index,
            "error": result.error,
        }

    @app.post("/api/seal/{capsule_id}/verify", dependencies=[Depends(verify_token)])
    async def seal_verify_endpoint(capsule_id: str) -> dict[str, Any]:
        """Run the five-check SoD verifier for a capsule's promote bundles."""
        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.policy_store import PolicyStore
        from novafabric.promote.verifier import verify_sod

        nova_data = Path(os.environ.get("NOVAFABRIC_HOME", str(Path.home() / ".novafabric")))
        bundle_store = PromoteBundleStore(nova_data)
        policy_store = PolicyStore(_seal_db_path())
        try:
            result = verify_sod(capsule_id, policy_store, bundle_store)
        finally:
            policy_store.close()

        if result.passed:
            check_results: list[dict[str, Any]] = [
                {"check": i + 1, "name": name, "passed": True, "message": "ok"}
                for i, name in enumerate(_SEAL_CHECK_NAMES)
            ]
        else:
            failed_idx = _SEAL_EXIT_TO_CHECK.get(result.exit_code, -1)
            check_results = []
            for i, name in enumerate(_SEAL_CHECK_NAMES):
                if i < failed_idx:
                    check_results.append(
                        {"check": i + 1, "name": name, "passed": True, "message": "ok"}
                    )
                elif i == failed_idx:
                    check_results.append(
                        {
                            "check": i + 1,
                            "name": name,
                            "passed": False,
                            "message": result.message,
                        }
                    )
                else:
                    break

        return {
            "capsule_id": capsule_id,
            "passed": result.passed,
            "exit_code": result.exit_code,
            "message": result.message,
            "check_results": check_results,
        }

    @app.post("/api/seal/{capsule_id}/bypass", dependencies=[Depends(verify_token)])
    async def seal_bypass_endpoint(capsule_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Create a time-limited SoD bypass, DSSE-signed and permanently logged (ADR-0059)."""
        import hashlib
        from datetime import UTC, datetime, timedelta

        from cryptography.x509 import NameOID, load_pem_x509_certificate

        from novafabric.promote.bundle_store import PromoteBundleStore
        from novafabric.promote.exceptions import PredicateValidationError
        from novafabric.promote.predicates import (
            BYPASS_PAYLOAD_TYPE,
            build_bypass_predicate,
            sign_promote_envelope,
            validate_predicate,
        )

        reason: str = body.get("reason", "")
        duration_hours: int = int(body.get("duration_hours", 24))
        key_pem: str = body.get("key_pem", "")
        cert_pem: str = body.get("cert_pem", "")
        target_env: str = body.get("target_env", "production")

        if len(reason) < 50:
            raise HTTPException(status_code=422, detail="reason must be at least 50 characters")
        if not (1 <= duration_hours <= 168):
            raise HTTPException(status_code=422, detail="duration_hours must be between 1 and 168")
        if not key_pem.strip():
            raise HTTPException(status_code=422, detail="key_pem is required")
        if not cert_pem.strip():
            raise HTTPException(status_code=422, detail="cert_pem is required")

        import json as _json
        import tempfile
        from pathlib import Path as _Path

        try:
            cert_obj = load_pem_x509_certificate(cert_pem.strip().encode())
            attrs = cert_obj.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
            authorized_by = (
                str(attrs[0].value) if attrs else cert_obj.subject.rfc4514_string()
            )
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"Failed to parse certificate: {exc}"
            ) from exc

        now = datetime.now(UTC)
        valid_until = now + timedelta(hours=duration_hours)
        valid_until_str = valid_until.isoformat()
        capsule_digest_hex = hashlib.sha256(capsule_id.encode()).hexdigest()

        predicate = build_bypass_predicate(
            capsule_id=capsule_id,
            capsule_digest_hex=capsule_digest_hex,
            target_environment=target_env,
            bypass_reason=reason,
            bypass_authorized_by=authorized_by,
            valid_until=valid_until_str,
            notification_sent_to=[],
            notification_status="not_configured",
        )

        try:
            validate_predicate("promote_bypass_v1.json", predicate)
        except PredicateValidationError as exc:
            raise HTTPException(
                status_code=422, detail=f"Schema validation error: {exc}"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as kf:
            kf.write(key_pem.strip().encode())
            key_path = _Path(kf.name)
        with tempfile.NamedTemporaryFile(suffix=".pem", delete=False) as cf:
            cf.write(cert_pem.strip().encode())
            cert_path = _Path(cf.name)
        try:
            payload_bytes = _json.dumps(predicate).encode()
            envelope = sign_promote_envelope(
                payload_bytes, BYPASS_PAYLOAD_TYPE, key_path, cert_path
            )
        except Exception as exc:
            raise HTTPException(
                status_code=422, detail=f"Signing error: {exc}"
            ) from exc
        finally:
            key_path.unlink(missing_ok=True)
            cert_path.unlink(missing_ok=True)

        nova_data = Path(os.environ.get("NOVAFABRIC_HOME", str(Path.home() / ".novafabric")))
        bundle_store = PromoteBundleStore(nova_data)
        bypass_uuid = bundle_store.put_bypass(capsule_id, envelope, valid_until_str)

        return {
            "bypass_uuid": bypass_uuid,
            "capsule_id": capsule_id,
            "authorized_by": authorized_by,
            "valid_until": valid_until_str,
            "target_env": target_env,
        }

    @app.get("/api/seal/log/verify", dependencies=[Depends(verify_token)])
    async def seal_log_verify_endpoint(
        capsule_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify internal consistency of the local Merkle log (ADR-0041).

        Recomputes all leaf hashes and checks the stored root hash against the
        recomputed root. Returns consistent=true when the log is intact.
        """
        from novafabric.trust.novaseal.merkle import MerkleLog

        db_path = _seal_db_path()
        log = MerkleLog(db_path)
        result = log.verify_consistency()

        response: dict[str, Any] = {
            "consistent": result.consistent,
            "entry_count": result.leaf_count,
            "message": "log is consistent" if result.consistent else (
                f"{len(result.errors)} error(s) found"
            ),
        }
        if capsule_id:
            # Check whether this capsule has at least one entry in the log.
            try:
                rows = log._conn.execute(  # noqa: SLF001
                    "SELECT COUNT(*) FROM leaves WHERE entry_json LIKE ?",
                    (f'%"{capsule_id}"%',),
                ).fetchone()
                response["capsule_included"] = bool(rows and rows[0] > 0)
            except Exception:
                response["capsule_included"] = None
        if result.errors:
            response["errors"] = result.errors

        return response

    # ---------- DB-KG-1: Capsule Knowledge Graph routes (v0.18.0) ----------

    def _kg_db_path() -> Path:
        env = os.environ.get("NOVA_KG_PATH")
        return Path(env) if env else (capsule_dir / ".nova" / "kg" / "nova_kg.kuzu")

    def _kg_ingest_tracker_path() -> Path:
        return _kg_db_path().parent / "ingest_tracker.db"

    # ---- KG auto-ingest: SQLite-backed tracking of already-ingested dirs ----
    # Keyed by the resolved string path of the capsule directory.
    # IngestTracker persists across serve restarts so idempotent re-ingest is
    # only triggered for directories not yet seen (rather than all on every boot).
    from novafabric.kg.ingest_tracker import IngestTracker as _IngestTracker  # noqa: PLC0415

    _kg_ingest_tracker: _IngestTracker | None = None

    def _get_kg_ingest_tracker() -> _IngestTracker:
        nonlocal _kg_ingest_tracker
        if _kg_ingest_tracker is None:
            _kg_ingest_tracker = _IngestTracker(_kg_ingest_tracker_path())
        return _kg_ingest_tracker

    # ---- TTL cache for /api/kg/topology ----
    # Avoids hammering KuzuDB with repeated graph serialisation queries from the
    # dashboard on every panel mount.  30-second TTL matches the stats cache.
    _topology_cache_data: dict[str, Any] | None = None
    _topology_cache_at: float | None = None
    _TOPOLOGY_CACHE_TTL = 30.0  # seconds

    def _ingest_one_capsule_dir(
        target: Path,
        *,
        store: Any,
        pipeline: Any,
    ) -> dict[str, int]:
        """Ingest one capsule directory into *store* via *pipeline*.

        Returns a dict with keys ``ingested``, ``skipped``, ``written``.
        Raises exceptions on hard errors; individual parse failures are counted
        in ``skipped`` rather than propagated.
        """
        import json as _json

        # Read model-calls.jsonl and tool-calls.jsonl explicitly (preferred).
        # Fall back to events.jsonl only when neither specialised file exists.
        event_files: list[Path] = []
        for fname in ("model-calls.jsonl", "tool-calls.jsonl"):
            p = target / fname
            if p.exists():
                event_files.append(p)
        if not event_files:
            fallback = target / "events.jsonl"
            if fallback.exists():
                event_files.append(fallback)

        if not event_files:
            return {"ingested": 0, "skipped": 0, "written": 0}

        ingested = 0
        skipped = 0
        for events_file in event_files:
            for raw in events_file.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = _json.loads(raw)
                    pipeline.ingest_event(ev, novaseal_valid=False)
                    ingested += 1
                except Exception:  # noqa: BLE001
                    skipped += 1
        written = pipeline.flush_to_store()
        return {"ingested": ingested, "skipped": skipped, "written": written}

    async def _kg_auto_ingest_loop() -> None:
        """Background asyncio task: auto-ingest new capsules into the KG.

        Behaviour:
        - Runs only when the KG store file exists (schema must have been
          initialised first via ``nova kg init`` or the /api/kg/init endpoint).
        - Scans *capsule_dir* for all capsule directories on each tick.
        - Skips directories already tracked in the SQLite IngestTracker (persists
          across serve restarts — see IngestTracker in kg/ingest_tracker.py).
        - Each failed ingest is logged as a warning; it does not stop other
          capsules from being processed.
        - Poll interval controlled by NOVA_KG_INGEST_INTERVAL env var (default 60s).
        - Uses asyncio.sleep so the event loop is never blocked.
        """
        while True:
            interval_seconds = float(os.environ.get("NOVA_KG_INGEST_INTERVAL", "60"))
            await asyncio.sleep(interval_seconds)
            kg_path = _kg_db_path()
            if not kg_path.exists():
                # KG not yet initialised — nothing to do.
                continue
            try:
                from novafabric.kg.pipeline import KGIngestionPipeline  # noqa: PLC0415
                from novafabric.kg.store import KGStore  # noqa: PLC0415
            except ImportError:
                # kuzu not installed — task becomes a quiet no-op.
                continue
            try:
                tracker = _get_kg_ingest_tracker()
                store = KGStore(str(kg_path))
                store.init_schema()
                pipeline = KGIngestionPipeline(store)
                capsule_dirs = discover_capsule_dirs(capsule_dir)
                newly_ingested: list[str] = []
                for cdir in capsule_dirs:
                    key = str(cdir.resolve())
                    if tracker.contains(key):
                        continue
                    try:
                        _fn = lambda d=cdir: _ingest_one_capsule_dir(  # noqa: E731
                            d, store=store, pipeline=pipeline
                        )
                        result = await asyncio.get_event_loop().run_in_executor(
                            None, _fn
                        )
                        tracker.mark(key)
                        newly_ingested.append(cdir.name)
                        if result["ingested"] > 0 or result["written"] > 0:
                            logger.info(
                                "KG auto-ingest: %s — ingested=%d skipped=%d written=%d",
                                cdir.name,
                                result["ingested"],
                                result["skipped"],
                                result["written"],
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "KG auto-ingest: failed to ingest %s — %s", cdir.name, exc
                        )
                if newly_ingested:
                    logger.debug(
                        "KG auto-ingest tick complete: %d new capsule(s) processed",
                        len(newly_ingested),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("KG auto-ingest tick error: %s", exc)

    async def _cost_auto_ingest_loop(interval_seconds: float = 120.0) -> None:
        """Background asyncio task: push new capsule cost events to ClickHouse.

        No-op when NOVA_CLICKHOUSE_URL is not set.  Runs every 2 minutes so
        the CostTab reflects recent runs without a manual trigger.
        """
        while True:
            await asyncio.sleep(interval_seconds)
            if not os.environ.get("NOVA_CLICKHOUSE_URL"):
                continue
            try:
                from novafabric.cost.clickhouse_store import ingest_all_capsules  # noqa: PLC0415

                home = Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))
                results = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: ingest_all_capsules(home)
                )
                total = sum(n for n in results.values() if n > 0)
                if total > 0:
                    logger.info(
                        "cost auto-ingest: %d new cost row(s) across %d run(s)",
                        total,
                        sum(1 for n in results.values() if n > 0),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("cost auto-ingest tick error: %s", exc)

    @app.get("/api/kg/status", dependencies=[Depends(verify_token)])
    async def kg_status_endpoint() -> dict[str, Any]:
        """Return Capsule KG store health + entity counts (DB-KG-1, ADR-0067)."""
        try:
            from novafabric.kg.store import KGStore
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=(
                    "Capsule KG not available: install with `pip install "
                    f"'novafabric[scale-kg]'` ({exc})"
                ),
            ) from exc
        path = _kg_db_path()
        if not path.exists():
            return {
                "store": "kuzu",
                "store_health": "not_initialised",
                "db_path": str(path),
                "edge_count": 0,
                "note": f"Run `nova kg init --path {path}` to create the store.",
            }
        return KGStore(path).get_status()

    @app.get("/api/kg/agents/{agent_id}/edges", dependencies=[Depends(verify_token)])
    async def kg_agent_edges_endpoint(agent_id: str) -> dict[str, Any]:
        """Return models, tools, and MCP servers called by an agent (DB-KG-1)."""
        try:
            from novafabric.kg.store import KGStore
        except ImportError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        path = _kg_db_path()
        if not path.exists():
            return {"agent_id": agent_id, "models": [], "tools": [], "mcp_servers": []}
        store = KGStore(path)
        return {
            "agent_id": agent_id,
            "models": store.query_agent_models(agent_id),
            "tools": store.query_agent_tools(agent_id),
            "mcp_servers": store.query_agent_mcp_servers(agent_id),
        }

    # ---------- DB-CAP-1: Capture-level policy routes (v0.18.0) ----------

    @app.get("/api/policy/capture-level", dependencies=[Depends(verify_token)])
    async def capture_level_get_endpoint() -> dict[str, Any]:
        """Return current capture level + tier descriptions (DB-CAP-1, cap-004)."""
        try:
            from novafabric.policies.capture_level import (
                CAPTURE_LEVEL_ALLOWLIST,
                CaptureLevel,
                CaptureLevelPolicy,
            )
        except ImportError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        policy = CaptureLevelPolicy.from_env()
        tiers = {
            level.value: sorted(CAPTURE_LEVEL_ALLOWLIST[level]) or ["(all fields)"]
            for level in CaptureLevel
        }
        return {
            "current_level": policy.level.value,
            "env_var": "NOVA_CAPTURE_LEVEL",
            "tiers": tiers,
            "note": (
                "Capture level is read at process start from NOVA_CAPTURE_LEVEL; "
                "restart required to change."
            ),
        }

    @app.post("/api/policy/capture-level", dependencies=[Depends(verify_token)])
    async def capture_level_set_endpoint(body: dict[str, Any]) -> dict[str, Any]:
        """Validate a capture-level value and return restart instructions (DB-CAP-1)."""
        try:
            from novafabric.policies.capture_level import CaptureLevel
        except ImportError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        level = body.get("level")
        if not isinstance(level, str):
            raise HTTPException(status_code=422, detail="body must include {level: str}")
        try:
            CaptureLevel(level)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"unknown level {level!r}; valid: {[lvl.value for lvl in CaptureLevel]}",
            ) from exc
        return {
            "ok": True,
            "level": level,
            "instruction": (
                f"Set NOVA_CAPTURE_LEVEL={level} in the server env and restart "
                "`nova serve`."
            ),
            "note": (
                "Server-side state is not mutated; this matches "
                "`nova policy capture-level set` CLI semantics."
            ),
        }

    # ---------- DB-ERA-1: GDPR erasure routes (v0.18.0) ----------

    def _cap003_enabled() -> bool:
        # Default true — OQ-01 resolved by ADR-0069 (AES-256-GCM DEK lifecycle).
        return os.environ.get("NOVA_CAP003_ENABLED", "true").lower() == "true"

    @app.post("/api/compliance/erasure/request", dependencies=[Depends(verify_token)])
    async def erasure_request_endpoint(body: dict[str, Any]) -> dict[str, Any]:
        """Queue a GDPR erasure request (DB-ERA-1, cap-003). Active — OQ-01 resolved by ADR-0069."""
        subject_id = body.get("subject_id") or body.get("run_id")
        if not isinstance(subject_id, str) or not subject_id.strip():
            raise HTTPException(
                status_code=422,
                detail="body must include subject_id or run_id (string)",
            )
        reason = body.get("reason", "gdpr_art_17")
        return {
            "ok": True,
            "subject_id": subject_id,
            "reason": reason,
            "state": "PENDING" if _cap003_enabled() else "FEATURE_FLAG_OFF",
            "cap003_enabled": _cap003_enabled(),
            "note": (
                "OQ-01 resolved by ADR-0069. Use `nova pii erase <subject_id>` "
                "for full DEK-destruction erasure. REST endpoint is informational stub."
            ),
        }

    @app.get("/api/compliance/erasure/status", dependencies=[Depends(verify_token)])
    async def erasure_status_endpoint(
        subject_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List GDPR erasure request status (DB-ERA-1). Stub."""
        return {
            "subject_id": subject_id,
            "cap003_enabled": _cap003_enabled(),
            "requests": [],
            "note": (
                "Erasure tracking requires ClickHouse + S3 GOVERNANCE mode (cap-003). "
                "Returns empty list until that infrastructure is wired in."
            ),
        }

    # ---------- DB-STG-1: Storage operations routes (v0.18.0) ----------

    @app.get("/api/storage/validate", dependencies=[Depends(verify_token)])
    async def storage_validate_endpoint(
        endpoint: str | None = Query(default=None),
        bucket: str = Query(default="nova-capsules"),
    ) -> dict[str, Any]:
        """Validate S3 backend supports Object Lock COMPLIANCE (DB-STG-1, cap-009)."""
        try:
            from novafabric.storage.nova_object_store import (
                NovaObjectStore,
                ObjectLockNotSupportedError,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"S3 abstraction not available: {exc}",
            ) from exc
        endpoint_url = endpoint or os.environ.get("NOVA_S3_ENDPOINT_URL")
        try:
            store = NovaObjectStore(endpoint_url=endpoint_url, bucket=bucket)
            info = store.validate()
            return {"ok": True, "endpoint": endpoint_url, "bucket": bucket, "result": info}
        except ObjectLockNotSupportedError as exc:
            return {
                "ok": False,
                "endpoint": endpoint_url,
                "bucket": bucket,
                "error": str(exc),
                "error_class": "ObjectLockNotSupportedError",
            }
        except Exception as exc:
            return {
                "ok": False,
                "endpoint": endpoint_url,
                "bucket": bucket,
                "error": str(exc),
                "error_class": type(exc).__name__,
            }

    @app.get("/api/storage/inspect/{run_id}", dependencies=[Depends(verify_token)])
    async def storage_inspect_endpoint(run_id: str) -> dict[str, Any]:
        """Show dual-object split for a run (DB-STG-1, cap-003)."""
        return {
            "run_id": run_id,
            "audit_object_key": f"{run_id}_audit.json",
            "pii_object_key": (
                f"{run_id}_pii.json" if _cap003_enabled() else None
            ),
            "cap003_enabled": _cap003_enabled(),
            "note": (
                "Object keys are computed from the run_id and the configured store layout. "
                "Inspect the actual object via `nova storage inspect --run-id <id>` in the CLI."
            ),
        }

    # ---------- DB-COST-1: Cost report routes (v0.19.0, cap-002) ----------

    @app.get("/api/cost/pricing", dependencies=[Depends(verify_token)])
    async def cost_pricing_endpoint() -> dict[str, Any]:
        """Return the per-1k-token price table from CostInterceptor (DB-COST-1)."""
        try:
            from novafabric.cost.interceptor import CostInterceptor
        except ImportError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        rows = [
            {
                "model": model,
                "input_price_per_1k_usd": prices[0],
                "output_price_per_1k_usd": prices[1],
            }
            for model, prices in CostInterceptor.PRICE_TABLE.items()
        ]
        return {
            "pricing": rows,
            "source": "novafabric.cost.interceptor.CostInterceptor.PRICE_TABLE",
            "note": "All prices are estimates; actual billing may differ.",
        }

    @app.get("/api/cost/report", dependencies=[Depends(verify_token)])
    async def cost_report_endpoint(
        run_id: str | None = Query(default=None),
        days: int = Query(default=7, ge=1, le=365),
    ) -> dict[str, Any]:
        """Return aggregated LLM cost (DB-COST-1 / cap-002).

        When ``NOVA_CLICKHOUSE_URL`` is set, queries ClickHouse.  Otherwise
        falls back to the local DuckDB accumulator (Evidence Fabric self-contained
        mode) so cost data is always available without external infrastructure.
        """
        import datetime as _dt  # noqa: PLC0415

        clickhouse_url = os.environ.get("NOVA_CLICKHOUSE_URL")
        if clickhouse_url:
            try:
                from novafabric.cost.clickhouse_store import (
                    cost_report as _ch_report,  # noqa: PLC0415
                )

                return await asyncio.get_event_loop().run_in_executor(
                    None, lambda: _ch_report(run_id=run_id, days=days)
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "backend": "clickhouse",
                    "error": str(exc),
                    "run_id": run_id,
                    "days": days,
                    "totals": {"input_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
                    "by_model": [],
                }

        # Self-contained fallback: query the DuckDB accumulator.
        try:
            from novafabric.evidence_fabric.duckdb_accumulator import (
                DuckDBAccumulator,  # noqa: PLC0415
            )

            _duckdb_path = Path(
                os.environ.get(
                    "NOVA_EVIDENCE_DUCKDB_PATH",
                    str(Path.home() / ".novafabric" / "evidence.duckdb"),
                )
            )
            _end = _dt.datetime.utcnow()
            _start = _end - _dt.timedelta(days=days)

            def _duckdb_query() -> dict[str, Any]:
                with DuckDBAccumulator(_duckdb_path) as _acc:
                    return _acc.query_cost_report(
                        tenant="default", start=_start, end=_end
                    )

            _by_model_raw = await asyncio.get_event_loop().run_in_executor(
                None, _duckdb_query
            )

            # Build response in the same shape as the ClickHouse path.
            by_model = [
                {
                    "model_id": model,
                    "provider": "unknown",
                    "input_tokens": v["tokens_in"],
                    "output_tokens": v["tokens_out"],
                    "cost_usd": v["cost_usd"],
                    "calls": v["calls"],
                }
                for model, v in _by_model_raw.items()
            ]
            total_input = sum(v["tokens_in"] for v in _by_model_raw.values())
            total_output = sum(v["tokens_out"] for v in _by_model_raw.values())
            total_cost = sum(v["cost_usd"] for v in _by_model_raw.values())
            return {
                "ok": True,
                "backend": "duckdb",
                "run_id": run_id,
                "days": days,
                "totals": {
                    "input_tokens": total_input,
                    "completion_tokens": total_output,
                    "cost_usd": round(total_cost, 6),
                },
                "by_model": sorted(by_model, key=lambda r: r["cost_usd"], reverse=True),
            }
        except Exception as exc:
            logger.warning("cost/report: DuckDB fallback failed: %s", exc)
            return {
                "ok": False,
                "backend": "duckdb",
                "error": str(exc),
                "run_id": run_id,
                "days": days,
                "totals": {"input_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0},
                "by_model": [],
            }

    # ---------- DB-SCH-1: Schema inspection route (v0.19.0, cap-001) ----------

    @app.get("/api/schema/list", dependencies=[Depends(verify_token)])
    async def schema_list_endpoint() -> dict[str, Any]:
        """List the CapsuleEventType values + meta (DB-SCH-1, cap-001 / ADR-0066;
        extended span taxonomy ADR-0082)."""
        try:
            from novafabric.schemas.event_schema import (
                CapsuleEventType,
                CostFacet,
                RunEnvelope,
            )
        except ImportError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc

        # Categorise event types by phase for the UI
        categories: dict[str, list[str]] = {
            "Run lifecycle": [],
            "Model calls": [],
            "Tool calls + permissions": [],
            "File + network": [],
            "Lineage": [],
            "Policy + governance": [],
            "Human-in-the-loop": [],
            "Privacy + security": [],
            "Agent spans": [],
            "Errors + misc": [],
        }
        category_map = {
            "RunStarted": "Run lifecycle",
            "RunCompleted": "Run lifecycle",
            "RunFailed": "Run lifecycle",
            "RunAborted": "Run lifecycle",
            "ModelCallStarted": "Model calls",
            "ModelCallCompleted": "Model calls",
            "ModelCallFailed": "Model calls",
            "ToolCallStarted": "Tool calls + permissions",
            "ToolCallCompleted": "Tool calls + permissions",
            "ToolCallFailed": "Tool calls + permissions",
            "ToolPermissionGranted": "Tool calls + permissions",
            "ToolPermissionDenied": "Tool calls + permissions",
            "FileReadEvent": "File + network",
            "FileWriteEvent": "File + network",
            "NetworkCallEvent": "File + network",
            "ArtifactProduced": "Lineage",
            "ArtifactConsumed": "Lineage",
            "EnvironmentLocked": "Lineage",
            "PolicyEvaluated": "Policy + governance",
            "HumanApprovalRequested": "Human-in-the-loop",
            "HumanApprovalGranted": "Human-in-the-loop",
            "HumanApprovalDenied": "Human-in-the-loop",
            "SecretRedacted": "Privacy + security",
            "PIIDetected": "Privacy + security",
            "StateTransition": "Agent spans",
            "MemoryOperation": "Agent spans",
            "GuardrailEvaluated": "Agent spans",
            "EvaluatorScored": "Agent spans",
            "RerankerApplied": "Agent spans",
            "VectorRetrievalStarted": "Agent spans",
            "VectorRetrievalCompleted": "Agent spans",
            "VectorRetrievalFailed": "Agent spans",
        }
        for ev in CapsuleEventType:
            cat = category_map.get(ev.value, "Errors + misc")
            categories[cat].append(ev.value)

        return {
            "schema_version": "1.0.0",
            "spec_path": "schemas/capsule-event-v1.schema.json",
            "event_types": [ev.value for ev in CapsuleEventType],
            "event_type_count": len(list(CapsuleEventType)),
            "categories": {k: v for k, v in categories.items() if v},
            "pydantic_models": [
                {
                    "name": "RunEnvelope",
                    "fields": list(RunEnvelope.model_fields.keys()),
                    "description": "Top-level envelope wrapping a capsule event.",
                },
                {
                    "name": "CostFacet",
                    "fields": list(CostFacet.model_fields.keys()),
                    "description": "LLM cost attribution facet (cap-002).",
                },
            ],
            "note": (
                "JSON Schema is shipped at "
                "schemas/capsule-event-v1.schema.json (draft 2020-12)."
            ),
        }

    # ---------- v0.19.0: KG init / ingest routes ----------

    @app.post("/api/kg/init", dependencies=[Depends(verify_token)])
    async def kg_init_endpoint() -> dict[str, Any]:
        """Initialise the KG schema (idempotent) — mirrors `nova kg init`."""
        kg_path = _kg_db_path()
        try:
            from novafabric.kg.store import KGStore
        except ImportError as exc:
            return {
                "ok": False, "db_path": str(kg_path), "error": str(exc),
                "note": "Install novafabric[scale-kg] to enable KG.",
            }
        try:
            kg_path.parent.mkdir(parents=True, exist_ok=True)
            store = KGStore(str(kg_path))
            store.init_schema()
            return {
                "ok": True, "db_path": str(kg_path),
                "note": "KG schema initialised (idempotent — safe to run again).",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "db_path": str(kg_path), "error": str(exc), "note": ""}

    @app.post("/api/kg/ingest", dependencies=[Depends(verify_token)])
    async def kg_ingest_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Ingest a capsule directory into the KG — mirrors `nova kg ingest`."""
        capsule_path_str: str = body.get("capsule_path", "")
        novaseal_verified: bool = bool(body.get("verified", False))
        if not capsule_path_str:
            raise HTTPException(status_code=422, detail="capsule_path is required")
        # Accept bare run_id (no slashes) — resolve to its capsule directory.
        if not capsule_path_str.startswith("/") and "/" not in capsule_path_str:
            target = _resolve_capsule(capsule_path_str, capsule_dir)
        else:
            target = Path(capsule_path_str)
            if not target.is_dir():
                raise HTTPException(
                    status_code=404,
                    detail=f"Directory not found: {capsule_path_str}",
                )
        kg_path = str(_kg_db_path())
        try:
            from novafabric.kg.pipeline import KGIngestionPipeline
            from novafabric.kg.store import KGStore
        except ImportError as exc:
            return {
                "ok": False, "error": str(exc),
                "note": "Install novafabric[scale-kg] to enable KG.",
            }
        try:
            import json as _json
            store = KGStore(kg_path)
            store.init_schema()
            pipeline = KGIngestionPipeline(store)
            events_file = None
            for candidate in ("model-calls.jsonl", "events.jsonl"):
                p = target / candidate
                if p.exists():
                    events_file = p
                    break
            if events_file is None:
                return {
                    "ok": False, "ingested": 0, "written": 0,
                    "error": "No events file found (expected model-calls.jsonl or events.jsonl)",
                    "note": "",
                }
            ingested = 0
            skipped = 0
            for raw in events_file.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = _json.loads(raw)
                    pipeline.ingest_event(ev, novaseal_valid=novaseal_verified)
                    ingested += 1
                except Exception:  # noqa: BLE001
                    skipped += 1
            written = pipeline.flush_to_store()
            return {
                "ok": True, "ingested": ingested, "skipped": skipped, "written": written,
                "capsule_path": capsule_path_str, "kg_path": kg_path, "note": "",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "ingested": 0, "written": 0, "error": str(exc), "note": ""}

    @app.post("/api/kg/detect", dependencies=[Depends(verify_token)])
    async def kg_detect_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Unsupervised SPKG anomaly scan of a capsule — mirrors `nova kg detect` (ADR-0111).

        Read-only. Ranks the most anomalous lineage edges by structural surprisal; every
        finding carries a MITRE ATT&CK technique (R2 — never a bare score). Pure-stdlib
        detector, so this needs no optional extra.
        """
        capsule_path_str: str = body.get("capsule_path", "")
        top: int = int(body.get("top", 5))
        if not capsule_path_str:
            raise HTTPException(status_code=422, detail="capsule_path is required")
        # Accept bare run_id (no slashes) — resolve to its capsule directory.
        if not capsule_path_str.startswith("/") and "/" not in capsule_path_str:
            target = _resolve_capsule(capsule_path_str, capsule_dir)
        else:
            target = Path(capsule_path_str)
            if not target.is_dir():
                raise HTTPException(
                    status_code=404,
                    detail=f"Directory not found: {capsule_path_str}",
                )
        from novafabric.kg.spkg.detect import StructuralAnomalyDetector, to_findings
        from novafabric.kg.spkg.provo_mapping import read_lineage_edges

        edges = read_lineage_edges(target)
        if not edges:
            return {"ok": True, "count": 0, "findings": [], "capsule_path": capsule_path_str}
        scored = StructuralAnomalyDetector().fit(edges).top_k(edges, k=top)
        findings = to_findings(scored)
        return {
            "ok": True,
            "count": len(findings),
            "findings": findings,
            "capsule_path": capsule_path_str,
        }

    def _spkg_parse_entity(spec: str) -> tuple[str, str]:
        if ":" not in spec:
            raise HTTPException(
                status_code=422,
                detail=f"entity must be 'kind:ref' (got {spec!r}), e.g. run:my-run",
            )
        kind, ref = spec.split(":", 1)
        return kind, ref

    @app.post("/api/kg/attack-path", dependencies=[Depends(verify_token)])
    async def kg_attack_path_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Shortest attack path between two entities — mirrors `nova kg attack-path` (UC2).

        Read-only. Builds an in-process SPKG LPG from the capsule's lineage, then runs a
        bounded shortest-path query. Requires the ``[spkg]`` extra.
        """
        capsule_path_str: str = body.get("capsule_path", "")
        from_entity: str = body.get("from_entity", "")
        to_entity: str = body.get("to_entity", "")
        max_depth: int = int(body.get("max_depth", 6))
        if not capsule_path_str or not from_entity or not to_entity:
            raise HTTPException(
                status_code=422,
                detail="capsule_path, from_entity, and to_entity are required",
            )
        if not capsule_path_str.startswith("/") and "/" not in capsule_path_str:
            target = _resolve_capsule(capsule_path_str, capsule_dir)
        else:
            target = Path(capsule_path_str)
            if not target.is_dir():
                raise HTTPException(
                    status_code=404, detail=f"Directory not found: {capsule_path_str}"
                )
        try:
            from novafabric.kg.spkg.graph_store import SpkgGraphStore
            from novafabric.kg.spkg.provo_mapping import read_lineage_edges
            from novafabric.lineage._types import node_id_for
        except ImportError as exc:
            return {"ok": False, "error": str(exc), "note": "Install novafabric[spkg]."}
        s_kind, s_ref = _spkg_parse_entity(from_entity)
        t_kind, t_ref = _spkg_parse_entity(to_entity)
        store = SpkgGraphStore()
        try:
            store.ingest_edges(read_lineage_edges(target))
            hops = store.attack_path(
                node_id_for(s_kind, s_ref), node_id_for(t_kind, t_ref), max_depth=max_depth
            )
        finally:
            store.close()
        return {
            "ok": True,
            "from_entity": from_entity,
            "to_entity": to_entity,
            "max_depth": max_depth,
            "path_found": hops is not None,
            "hops": hops,
        }

    @app.post("/api/kg/blast-radius", dependencies=[Depends(verify_token)])
    async def kg_blast_radius_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Impact/blast-radius of an entity — mirrors `nova kg blast-radius` (UC3).

        Read-only. ``downstream`` (default) lists what the entity affects; ``upstream``
        lists its provenance. Requires the ``[spkg]`` extra.
        """
        capsule_path_str: str = body.get("capsule_path", "")
        entity: str = body.get("entity", "")
        upstream: bool = bool(body.get("upstream", False))
        max_depth: int = int(body.get("max_depth", 6))
        if not capsule_path_str or not entity:
            raise HTTPException(
                status_code=422, detail="capsule_path and entity are required"
            )
        if not capsule_path_str.startswith("/") and "/" not in capsule_path_str:
            target = _resolve_capsule(capsule_path_str, capsule_dir)
        else:
            target = Path(capsule_path_str)
            if not target.is_dir():
                raise HTTPException(
                    status_code=404, detail=f"Directory not found: {capsule_path_str}"
                )
        try:
            from novafabric.kg.spkg.graph_store import SpkgGraphStore
            from novafabric.kg.spkg.provo_mapping import read_lineage_edges
            from novafabric.lineage._types import node_id_for
        except ImportError as exc:
            return {"ok": False, "error": str(exc), "note": "Install novafabric[spkg]."}
        kind, ref = _spkg_parse_entity(entity)
        store = SpkgGraphStore()
        try:
            store.ingest_edges(read_lineage_edges(target))
            node_id = node_id_for(kind, ref)
            affected = (
                store.ancestors(node_id, max_depth)
                if upstream
                else store.descendants(node_id, max_depth)
            )
        finally:
            store.close()
        return {
            "ok": True,
            "entity": entity,
            "direction": "upstream" if upstream else "downstream",
            "max_depth": max_depth,
            "count": len(affected),
            "entities": [
                {"kind": k, "ref": r}
                for _nid, k, r in sorted(affected, key=lambda x: (x[1], x[2]))
            ],
        }

    @app.post("/api/kg/ingest-all", dependencies=[Depends(verify_token)])
    async def kg_ingest_all_endpoint(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Bulk-ingest all capsule directories into the KG.

        Scans *capsule_base* (defaults to the server's capsule_dir) for
        subdirectories and ingests each one.  Already-ingested directories
        tracked in the IngestTracker are skipped.

        Returns ``{ok, total, newly_ingested, skipped, failed, capsule_base, kg_path}``.
        """
        capsule_base_str: str = body.get("capsule_base", "") if body else ""
        base: Path = Path(capsule_base_str) if capsule_base_str else capsule_dir
        if not base.is_dir():
            raise HTTPException(
                status_code=404,
                detail=f"Capsule base directory not found: {base}",
            )

        kg_path = str(_kg_db_path())
        # KG ingest reads event files directly (not capsule.yaml), so discover
        # by ingestable event files — a strict superset of the manifest scan.
        dirs = discover_ingestable_dirs(base)

        try:
            from novafabric.kg.pipeline import KGIngestionPipeline  # noqa: PLC0415
            from novafabric.kg.store import KGStore  # noqa: PLC0415

            tracker = _get_kg_ingest_tracker()
            store = KGStore(kg_path)
            store.init_schema()
            pipeline = KGIngestionPipeline(store)
        except ImportError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "note": "Install novafabric[scale-kg] to enable KG.",
                "total": len(dirs),
                "newly_ingested": 0,
                "skipped": len(dirs),
                "failed": 0,
            }

        newly_ingested = 0
        skipped = 0
        failed = 0

        for cdir in dirs:
            key = str(cdir.resolve())
            if tracker.contains(key):
                skipped += 1
                continue
            try:
                result = _ingest_one_capsule_dir(cdir, store=store, pipeline=pipeline)
                tracker.mark(key)
                if result["ingested"] > 0 or result["written"] > 0:
                    newly_ingested += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("kg ingest-all: failed %s — %s", cdir.name, exc)
                failed += 1

        return {
            "ok": True,
            "total": len(dirs),
            "newly_ingested": newly_ingested,
            "skipped": skipped,
            "failed": failed,
            "capsule_base": str(base),
            "kg_path": kg_path,
        }

    # ---------- v1/kg/ — bearer-auth REST endpoints (ADR-0067 v1.2) ----------

    @app.get("/v1/kg/status", dependencies=[Depends(verify_token)])
    async def v1_kg_status_endpoint() -> dict[str, Any]:
        """KG store health check (Tier 2+ aware).  Alias of /api/kg/status."""
        try:
            from novafabric.kg.store import KGStore  # noqa: PLC0415
        except ImportError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "note": "Install `novafabric[scale-kg]` to enable KG.",
            }
        path = _kg_db_path()
        if not path.exists():
            return {
                "ok": False,
                "store_health": "uninitialised",
                "note": f"Run `nova kg init --path {path}` to create the store.",
            }
        try:
            store = KGStore(db_path=path)
            status = store.get_status()
            return {"ok": True, **status}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.get("/v1/kg/query", dependencies=[Depends(verify_token)])
    async def v1_kg_query_endpoint(
        agent_id: str,
    ) -> dict[str, Any]:
        """Query models and tools for an agent.  Mirrors `nova kg query`."""
        try:
            from novafabric.kg.store import KGStore  # noqa: PLC0415
        except ImportError as exc:
            return {"ok": False, "error": str(exc)}
        path = _kg_db_path()
        if not path.exists():
            return {"ok": False, "agent_id": agent_id, "models": [], "tools": []}
        try:
            store = KGStore(db_path=path)
            models = store.query_agent_models(agent_id)
            tools = store.query_agent_tools(agent_id)
            return {"ok": True, "agent_id": agent_id, "models": models, "tools": tools}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "agent_id": agent_id, "error": str(exc)}

    @app.get("/api/kg/topology", dependencies=[Depends(verify_token)])
    async def kg_topology_endpoint(max_nodes: int = 500) -> dict[str, Any]:
        """Return all KG nodes and edges for multi-layer topology visualization.

        Returns node/edge lists with type annotations and count breakdowns per layer.
        Caps at *max_nodes* total nodes (default 500) to protect dashboard rendering.
        Response is cached for 30 seconds (TOPOLOGY_CACHE_TTL) to reduce KuzuDB load.
        """
        nonlocal _topology_cache_data, _topology_cache_at
        # Serve from TTL cache when fresh (avoids repeated KuzuDB graph serialisation).
        if (
            _topology_cache_data is not None
            and _topology_cache_at is not None
            and time.monotonic() - _topology_cache_at < _TOPOLOGY_CACHE_TTL
        ):
            return _topology_cache_data
        try:
            from novafabric.kg.store import KGStore  # noqa: PLC0415
        except ImportError as exc:
            return {"ok": False, "error": str(exc)}
        path = _kg_db_path()
        if not path.exists():
            return {
                "ok": False,
                "note": f"KG not initialised — run `nova kg init` (expected at {path})",
                "nodes": [],
                "edges": [],
                "node_counts": {},
                "edge_counts": {},
            }
        try:
            store = KGStore(db_path=path)
            graph = store.get_topology_graph(max_nodes=max_nodes)
            result: dict[str, Any] = {"ok": True, **graph}
            _topology_cache_data = result
            _topology_cache_at = time.monotonic()
            return result
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False, "error": str(exc),
                "nodes": [], "edges": [], "node_counts": {}, "edge_counts": {},
            }

    @app.get("/v1/kg/audit", dependencies=[Depends(verify_token)])
    async def v1_kg_audit_endpoint() -> dict[str, Any]:
        """KG health audit: node/edge counts, zero-call-count edges.

        Mirrors `nova kg audit`.  Returns 200 regardless of issue count —
        callers should check the ``issues`` field in the response body.
        """
        try:
            from novafabric.kg.store import KGStore  # noqa: PLC0415
        except ImportError as exc:
            return {"ok": False, "error": str(exc)}
        path = _kg_db_path()
        if not path.exists():
            return {
                "ok": False,
                "store_health": "uninitialised",
                "note": f"Run `nova kg init --path {path}` to create the store.",
            }
        try:
            store = KGStore(db_path=path)
            audit = store.get_audit()
            return {"ok": True, **audit}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ---------- B-2: KG Tier 2/3 — alias table + entity review queue ----------

    def _alias_db_path() -> Path:
        env = os.environ.get("NOVA_KG_ALIAS_DB", "")
        return Path(env) if env else (capsule_dir / ".nova" / "kg" / "alias.db")

    def _queue_db_path() -> Path:
        env = os.environ.get("NOVA_KG_QUEUE_DB", "")
        return Path(env) if env else (capsule_dir / ".nova" / "kg" / "review_queue.db")

    @app.get("/api/kg/aliases", dependencies=[Depends(verify_token)])
    async def kg_aliases_list_endpoint(
        canonical: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List all alias-table entries, optionally filtered by canonical entity id."""
        import sqlite3 as _sqlite3

        db_path = _alias_db_path()
        if not db_path.exists():
            return {
                "ok": True,
                "count": 0,
                "aliases": [],
                "note": "Alias DB not initialised yet.",
            }
        conn = _sqlite3.connect(str(db_path))
        conn.row_factory = _sqlite3.Row
        try:
            if canonical:
                rows = conn.execute(
                    "SELECT alias, canonical, entity_type, confidence, source, created_at"
                    " FROM alias_table WHERE canonical = ? ORDER BY alias",
                    (canonical,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT alias, canonical, entity_type, confidence, source, created_at"
                    " FROM alias_table ORDER BY canonical, alias"
                ).fetchall()
        finally:
            conn.close()
        aliases = [dict(r) for r in rows]
        return {"ok": True, "count": len(aliases), "aliases": aliases}

    @app.post("/api/kg/aliases", dependencies=[Depends(verify_token)])
    async def kg_aliases_register_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Register (upsert) an alias into the Tier-2 alias table."""
        from datetime import datetime, timezone

        from novafabric.kg.alias_resolver import AliasEntry, AliasTableResolver

        alias_val = str(body.get("alias", "")).strip()
        canonical_val = str(body.get("canonical", "")).strip()
        entity_type_val = str(body.get("entity_type", "")).strip()
        if not alias_val or not canonical_val or not entity_type_val:
            return {"ok": False, "error": "alias, canonical, and entity_type are required"}
        resolver = AliasTableResolver(db_path=_alias_db_path())
        entry = AliasEntry(
            alias=alias_val,
            canonical=canonical_val,
            entity_type=entity_type_val,
            confidence=float(body.get("confidence", 1.0)),
            source=str(body.get("registered_by", "api")),
            created_at=datetime.now(timezone.utc),
        )
        try:
            resolver.register(entry)
        finally:
            resolver.close()
        return {
            "ok": True,
            "alias": alias_val,
            "canonical": canonical_val,
            "entity_type": entity_type_val,
        }

    @app.get("/api/kg/entity-queue", dependencies=[Depends(verify_token)])
    async def kg_entity_queue_list_endpoint() -> dict[str, Any]:
        """Return all pending ReviewItems from the Tier-3 human review queue."""
        from novafabric.kg.review_queue import HumanReviewQueueWriter

        q = HumanReviewQueueWriter(db_path=_queue_db_path())
        try:
            items = q.list_pending()
        finally:
            q.close()
        return {
            "ok": True,
            "count": len(items),
            "items": [i.model_dump(mode="json") for i in items],
        }

    @app.get("/api/kg/entity-queue/stats", dependencies=[Depends(verify_token)])
    async def kg_entity_queue_stats_endpoint() -> dict[str, Any]:
        """Return pending/approved/rejected counts for the entity review queue."""
        from novafabric.kg.review_queue import HumanReviewQueueWriter

        q = HumanReviewQueueWriter(db_path=_queue_db_path())
        try:
            stats = q.stats()
        finally:
            q.close()
        return {"ok": True, **stats}

    @app.post(
        "/api/kg/entity-queue/{item_id}/approve", dependencies=[Depends(verify_token)]
    )
    async def kg_entity_queue_approve_endpoint(
        item_id: str,
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Approve a review item.  Body: {canonical: str, resolved_by: str}."""
        canonical = body.get("canonical", "")
        resolved_by = body.get("resolved_by", "api")
        if not canonical:
            raise HTTPException(status_code=422, detail="canonical is required")
        from novafabric.kg.review_queue import HumanReviewQueueWriter

        q = HumanReviewQueueWriter(db_path=_queue_db_path())
        try:
            q.approve(item_id, canonical=canonical, resolved_by=resolved_by)
        finally:
            q.close()
        return {"ok": True, "item_id": item_id, "canonical": canonical}

    @app.post(
        "/api/kg/entity-queue/{item_id}/reject", dependencies=[Depends(verify_token)]
    )
    async def kg_entity_queue_reject_endpoint(
        item_id: str,
        body: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        """Reject a review item.  Body: {resolved_by: str}."""
        resolved_by = body.get("resolved_by", "api") if body else "api"
        from novafabric.kg.review_queue import HumanReviewQueueWriter

        q = HumanReviewQueueWriter(db_path=_queue_db_path())
        try:
            q.reject(item_id, resolved_by=resolved_by)
        finally:
            q.close()
        return {"ok": True, "item_id": item_id, "status": "rejected"}

    # ---------- v0.19.0: run utilities ----------

    @app.get("/api/admin/new-run-id", dependencies=[Depends(verify_token)])
    async def new_run_id_endpoint() -> dict[str, Any]:
        """Generate a fresh ULID for use as NOVAFABRIC_GLOBAL_RUN_ID (cap-007 FR-27)."""
        from novafabric.capsule.ulid_util import new_ulid
        run_id = new_ulid()
        return {
            "run_id": run_id,
            "env_var": "NOVAFABRIC_GLOBAL_RUN_ID",
            "cli_cmd": f"NOVAFABRIC_GLOBAL_RUN_ID={run_id} nova capture ...",
            "note": (
                "Copy this ID and pass it as NOVAFABRIC_GLOBAL_RUN_ID"
                " before calling nova capture."
            ),
        }

    @app.get("/api/runs/{run_id}/children", dependencies=[Depends(verify_token)])
    async def get_run_children_endpoint(run_id: str) -> dict[str, Any]:
        """Return parent capsule metadata + list of child capsule summaries."""
        parent_dir = capsule_dir / run_id
        if not parent_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Capsule directory not found: {run_id}")
        manifest_path = parent_dir / "capsule.yaml"
        if not manifest_path.exists():
            manifest_path = parent_dir / "capsule.json"
        import yaml as _yaml
        try:
            raw = manifest_path.read_text(encoding="utf-8")
            manifest = (
                _yaml.safe_load(raw) if manifest_path.suffix == ".yaml"
                else __import__("json").loads(raw)
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"Cannot parse manifest: {exc}") from exc
        children: list[dict[str, Any]] = []
        for child_dir in sorted(capsule_dir.iterdir()):
            if child_dir == parent_dir or not child_dir.is_dir():
                continue
            lineage_file = child_dir / "lineage.jsonl"
            if lineage_file.exists():
                import json as _json
                for line in lineage_file.read_text(encoding="utf-8").splitlines():
                    try:
                        rec = _json.loads(line)
                        if rec.get("parent_run_id") == run_id or rec.get("parent_id") == run_id:
                            child_manifest_p = child_dir / "capsule.yaml"
                            if not child_manifest_p.exists():
                                child_manifest_p = child_dir / "capsule.json"
                            if child_manifest_p.exists():
                                try:
                                    _txt = child_manifest_p.read_text()
                                    cm = (
                                        _yaml.safe_load(_txt)
                                        if child_manifest_p.suffix == ".yaml"
                                        else __import__("json").loads(_txt)
                                    )
                                    children.append({
                                        "run_id": child_dir.name,
                                        "status": cm.get("status"),
                                        "edge_type": rec.get("edge_type"),
                                        "exit_code": cm.get("exit_code"),
                                    })
                                except Exception:  # noqa: BLE001
                                    children.append({
                                        "run_id": child_dir.name,
                                        "status": "unknown",
                                        "edge_type": rec.get("edge_type"),
                                        "exit_code": None,
                                    })
                            break
                    except Exception:  # noqa: BLE001
                        continue
        return {
            "run_id": run_id,
            "status": manifest.get("status"),
            "exit_code": manifest.get("exit_code"),
            "child_count": len(children),
            "children": children,
        }

    @app.post("/api/runs/{run_id}/validate-distributed", dependencies=[Depends(verify_token)])
    async def validate_distributed_endpoint(run_id: str) -> dict[str, Any]:
        """Validate a distributed parent capsule + its workers (nova run validate-distributed)."""
        parent_dir = capsule_dir / run_id
        if not parent_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Capsule directory not found: {run_id}")
        try:
            from novafabric.capsule.validator import DistributedCapsuleValidator
        except ImportError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        try:
            validator = DistributedCapsuleValidator(parent_dir)
            result = validator.validate_distributed(run_id)
            return {
                "ok": result.exit_code == 0,
                "status": result.status,
                "message": result.message,
                "exit_code": result.exit_code,
                "run_id": run_id,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False, "status": "ERROR",
                "message": str(exc), "exit_code": 1, "run_id": run_id,
            }

    # ---------- v0.27.0: capsule delete ----------

    @app.delete("/api/runs/{run_id}", dependencies=[Depends(verify_token)])
    async def delete_run_endpoint(
        run_id: str,
        force: bool = Query(False),
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Delete a capsule directory, subject to legal holds and retention policy."""
        if not run_id or ".." in run_id or "/" in run_id:
            raise HTTPException(status_code=400, detail="invalid run_id")

        cap_dir = capsule_dir / run_id
        if not cap_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"Capsule not found: {run_id}")

        # Check active legal holds across all registries (hold always blocks, even with force=True)
        active_holds: list[str] = []
        registries_base = capsule_dir.parent / "registries"
        if registries_base.exists():
            for reg_dir in registries_base.iterdir():
                if not reg_dir.is_dir():
                    continue
                holds_path = reg_dir / "holds.jsonl"
                if not holds_path.exists():
                    continue
                for line in holds_path.read_text().splitlines():
                    if line.strip():
                        h = json.loads(line)
                        if h.get("released_at") is None:
                            active_holds.append(h["hold_id"])
        if active_holds:
            hold_ids = active_holds[:3]
            detail = f"Deletion blocked by {len(active_holds)} active legal hold(s): {hold_ids}"
            raise HTTPException(status_code=409, detail=detail)

        import shutil  # noqa: PLC0415

        shutil.rmtree(cap_dir)
        audit.append(
            action="capsule_delete",
            args={"run_id": run_id, "force": force},
            cli_equivalent=f"nova capsule delete {run_id}" + (" --force" if force else ""),
            actor_token_fp=actor_fp,
        )
        return {"ok": True, "run_id": run_id, "note": f"Capsule {run_id} deleted"}

    # ---------- v0.20.0: unregister, doctor, policy test/explain, audit extensions ----------

    @app.delete("/api/assets/{name}/{version}", dependencies=[Depends(verify_token)])
    async def unregister_asset_endpoint(
        name: str,
        version: str,
        force: bool = False,
        actor_fp: str = Depends(verify_token),
    ) -> dict[str, Any]:
        """Hard-delete an asset from the registry (nova unregister)."""
        from novafabric.registry.service import (
            AssetNotFoundError,
            UnregisterBlockedError,
            unregister_asset,
        )
        try:
            snapshot = unregister_asset(name, version, actor=actor_fp, force=force, db_path=db_path)
        except AssetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except UnregisterBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        audit.append(
            action="unregister_asset",
            args={"name": name, "version": version, "force": force},
            cli_equivalent=f"nova unregister {name}@{version}" + (" --force" if force else ""),
            actor_token_fp=actor_fp,
        )
        return {"ok": True, "name": name, "version": version, "snapshot": snapshot}

    @app.get("/api/doctor", dependencies=[Depends(verify_token)])
    async def doctor_endpoint() -> dict[str, Any]:
        """Run diagnostic checks on the NovaFabric installation (nova doctor)."""
        import shutil
        import sys

        checks: list[dict[str, Any]] = []

        checks.append({
            "name": "capsule_dir",
            "ok": capsule_dir.is_dir(),
            "detail": str(capsule_dir),
        })

        try:
            from novafabric.registry.store import get_connection, init_schema
            conn = get_connection(db_path)
            init_schema(conn)
            row_count = conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            conn.close()
            checks.append({
                "name": "registry_db", "ok": True,
                "detail": f"{row_count} assets",
                "db_path": str(db_path or "~/.novafabric/registry.db"),
            })
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "registry_db", "ok": False, "detail": str(exc)})

        try:
            from novafabric.lineage._store import LineageStore
            ls = LineageStore(db_path=db_path)
            edge_count = ls._conn.execute("SELECT COUNT(*) FROM lineage_edges").fetchone()[0]
            checks.append({"name": "lineage_store", "ok": True, "detail": f"{edge_count} edges"})
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": "lineage_store", "ok": False, "detail": str(exc)})

        opa_bin = shutil.which("opa")
        checks.append({
            "name": "opa_binary", "ok": opa_bin is not None, "detail": opa_bin or "not found",
        })

        seal_db = _seal_db_path()
        checks.append({"name": "novaseal_db", "ok": seal_db.exists(), "detail": str(seal_db)})

        kg_path = _kg_db_path()
        if not kg_path.exists():
            checks.append({
                "name": "kg_store", "ok": False,
                "detail": f"not initialised — run `nova kg init` (expected at {kg_path})",
            })
        else:
            try:
                from novafabric.kg.store import KGStore
                kg = KGStore(db_path=kg_path)
                status = kg.get_status()
                store_health = status.get("store_health", "unknown")
                edge_count_kg = status.get("edge_count", 0)
                checks.append({
                    "name": "kg_store",
                    "ok": store_health == "ok",
                    "detail": f"{store_health} · {edge_count_kg} edges",
                })
            except Exception as exc:  # noqa: BLE001
                checks.append({"name": "kg_store", "ok": False, "detail": str(exc)})

        checks.append({
            "name": "python_version",
            "ok": sys.version_info >= (3, 10),
            "detail": sys.version,
        })

        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    @app.post("/api/policy/test", dependencies=[Depends(verify_token)])
    async def policy_test_endpoint(body: dict[str, Any]) -> dict[str, Any]:
        """Run the Rego test suite for the policy bundle (nova policy test)."""
        import shutil
        import subprocess

        opa_bin = shutil.which("opa")
        if not opa_bin:
            return {
                "ok": False, "backend": "stub",
                "reason": "OPA binary not found — install opa to run policy tests",
                "output": "",
            }
        try:
            from novafabric.policy._opa_engine import _get_bundle_default
            default_bundle = str(_get_bundle_default())
        except Exception:  # noqa: BLE001
            default_bundle = str(Path.home() / ".novafabric" / "policy" / "bundle")

        bundle_path = body.get("bundle_path") or default_bundle
        try:
            proc = subprocess.run(
                [opa_bin, "test", bundle_path, "--verbose"],
                capture_output=True, text=True, timeout=30,
            )
            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "bundle_path": bundle_path,
                "output": proc.stdout + proc.stderr,
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False, "exit_code": -1, "bundle_path": bundle_path,
                "output": "timed out after 30s",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "exit_code": -1, "bundle_path": bundle_path, "output": str(exc)}

    @app.get("/api/policy/recent-decisions", dependencies=[Depends(verify_token)])
    async def policy_recent_decisions_endpoint(
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """Return recent decision IDs from the audit log for autocomplete."""
        import json as _json
        audit_path = Path.home() / ".novafabric" / "dashboard-audit.jsonl"
        if not audit_path.exists():
            return {"decision_ids": []}
        seen: list[str] = []
        for line in reversed(audit_path.read_text(encoding="utf-8").splitlines()):
            try:
                entry = _json.loads(line)
                args = entry.get("args") or {}
                extra = entry.get("extra") or {}
                did = (
                    args.get("decision_id")
                    or extra.get("decision_id")
                    or entry.get("audit_id")
                )
                if did and did not in seen:
                    seen.append(did)
                    if len(seen) >= limit:
                        break
            except Exception:  # noqa: BLE001
                continue
        return {"decision_ids": seen}

    @app.get("/api/policy/explain", dependencies=[Depends(verify_token)])
    async def policy_explain_endpoint(decision_id: str) -> dict[str, Any]:
        """Look up a past policy decision from the audit log (nova policy explain)."""
        import json as _json
        audit_path = Path.home() / ".novafabric" / "dashboard-audit.jsonl"
        if not audit_path.exists():
            return {"ok": False, "reason": "audit log not found", "entries": [], "count": 0}
        matches: list[dict[str, Any]] = []
        for line in audit_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = _json.loads(line)
                args = entry.get("args") or {}
                extra = entry.get("extra") or {}
                if (
                    entry.get("audit_id") == decision_id
                    or args.get("decision_id") == decision_id
                    or extra.get("decision_id") == decision_id
                    or str(args.get("decision_id", "")).startswith(decision_id)
                ):
                    matches.append(entry)
            except Exception:  # noqa: BLE001
                continue
        return {"ok": True, "decision_id": decision_id, "entries": matches, "count": len(matches)}

    @app.get("/api/compliance/audit/coverage", dependencies=[Depends(verify_token)])
    async def compliance_audit_coverage_endpoint(
        profile: str = "nist-ai-rmf",
        threshold: float = 0.8,
    ) -> dict[str, Any]:
        """Per-control coverage analysis for the local capsule store (nova audit coverage)."""
        try:
            from novafabric.compliance.audit.engine import AuditEngine
            from novafabric.compliance.audit.loader import load_profile
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"compliance audit module not available: {exc}",
            ) from exc
        try:
            ctrl_profile = load_profile(profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            engine = AuditEngine(data_dir=capsule_dir.parent, profile=ctrl_profile)
            report = engine.scan()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"audit scan failed: {exc}") from exc
        return {
            "profile": profile,
            "overall_score": report.overall_score,
            "threshold": threshold,
            "threshold_met": report.overall_score >= threshold,
            "capsule_count": report.capsule_count,
            "total_controls": report.total_controls,
            "covered_controls": report.covered_controls,
            "partial_controls": report.partial_controls,
            "missing_controls": report.missing_controls,
            "coverages": [cov.model_dump(by_alias=True) for cov in report.coverages],
        }

    @app.post("/api/compliance/audit/bundle", dependencies=[Depends(verify_token)])
    async def compliance_audit_bundle_endpoint(body: dict[str, Any]) -> dict[str, Any]:
        """Export a compliance evidence bundle as base64-encoded ZIP (nova audit bundle)."""
        import base64
        import io
        import json as _json
        import zipfile

        try:
            from novafabric.compliance.audit.engine import AuditEngine
            from novafabric.compliance.audit.loader import load_profile
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"compliance audit module not available: {exc}",
            ) from exc
        profile = body.get("profile", "nist-ai-rmf")
        try:
            ctrl_profile = load_profile(profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            engine = AuditEngine(data_dir=capsule_dir.parent, profile=ctrl_profile)
            report = engine.scan()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"audit scan failed: {exc}") from exc

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            report_json = _json.dumps(report.model_dump(by_alias=True, mode="json"), indent=2)
            zf.writestr("audit-report.json", report_json)
            for cov in report.coverages:
                for ev_path in getattr(cov, "evidence_paths", []) or []:
                    p = Path(ev_path)
                    if p.exists():
                        try:
                            zf.write(p, arcname=f"evidence/{p.name}")
                        except Exception:  # noqa: BLE001
                            pass
        raw = buf.getvalue()
        filename = f"nova-audit-{profile}-{report.report_id[:8]}.zip"
        return {
            "ok": True,
            "profile": profile,
            "report_id": report.report_id,
            "overall_score": report.overall_score,
            "capsule_count": report.capsule_count,
            "filename": filename,
            "content_base64": base64.b64encode(raw).decode(),
            "size_bytes": len(raw),
        }

    @app.post("/api/compliance/audit/verify", dependencies=[Depends(verify_token)])
    async def compliance_audit_verify_endpoint(body: dict[str, Any]) -> dict[str, Any]:
        """Validate the structure of an audit report JSON-LD (nova audit verify)."""
        import json as _json

        try:
            from novafabric.compliance.audit.models import AuditReport
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"compliance audit module not available: {exc}",
            ) from exc
        report_data = body.get("report")
        if not report_data:
            raise HTTPException(status_code=422, detail="report field required")
        if isinstance(report_data, str):
            try:
                report_data = _json.loads(report_data)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=422, detail=f"report is not valid JSON: {exc}"
                ) from exc
        try:
            report = AuditReport.model_validate(report_data)
            return {
                "ok": True, "valid": True,
                "report_id": report.report_id,
                "profile": report.profile_id,
                "overall_score": report.overall_score,
                "errors": [],
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "valid": False, "errors": [str(exc)]}

    # ---------- Assurance — OWASP LLM evidence report (E-10 / nova assure) ----------

    @app.get("/api/assure/{run_id}", dependencies=[Depends(verify_token)])
    async def api_assure(run_id: str, capsule_path: str | None = None) -> dict[str, Any]:
        """Run OWASP Top 10 for LLM evidence checks against a capsule."""
        # Resolve capsule path: explicit param > auto-discover from capsule_dir
        if capsule_path:
            cap = Path(capsule_path)
        else:
            cap = capsule_dir / run_id
            if not cap.exists() or not (cap / "capsule.yaml").exists():
                # Fallback: scan capsule_dir for matching run_id in manifest
                cap_found: Path | None = None
                for d in discover_capsule_dirs(capsule_dir):
                    try:
                        m = load_capsule_manifest(d)
                    except FileNotFoundError:
                        continue
                    if m.get("run_id") == run_id:
                        cap_found = d
                        break
                if cap_found is not None:
                    cap = cap_found

        if not cap.exists() or not (cap / "capsule.yaml").exists():
            return {
                "ok": False,
                "backend": "stub",
                "reason": f"Capsule not found for run_id={run_id}",
            }

        try:
            from novafabric.assure.checker import AssuranceChecker
            checker = AssuranceChecker()
            report = checker.check_all(cap)
            return report.model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ---------- MCP supply-chain risk scanner (E-9 / nova mcp scan) ----------

    @app.post("/api/mcp/scan", dependencies=[Depends(verify_token)])
    async def api_mcp_scan(body: MCPScanRequest = Body(...)) -> dict[str, Any]:
        """Scan an MCP server manifest for OWASP LLM supply-chain risks."""
        try:
            from novafabric.mcp_scanner.scanner import RiskScanner
            scanner = RiskScanner()
            report = scanner.scan(body.manifest)
            return report.model_dump_report()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ---------- MCP risk-report (nova mcp risk-report — structured OWASP report) ----------

    @app.post("/api/mcp/risk-report", dependencies=[Depends(verify_token)])
    async def mcp_risk_report_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Generate a structured OWASP LLM risk report for an MCP manifest.

        Mirrors ``nova mcp risk-report``.
        """
        manifest: dict[str, Any] = body.get("manifest", {})
        if not manifest:
            raise HTTPException(status_code=422, detail="manifest is required")
        try:
            import json as _json  # noqa: PLC0415
            import tempfile  # noqa: PLC0415
            from pathlib import Path as _Path  # noqa: PLC0415

            from novafabric.mcp_scanner.scanner import RiskScanner  # noqa: PLC0415
            scanner = RiskScanner()
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as tf:
                _json.dump(manifest, tf)
                tmp_path = _Path(tf.name)
            try:
                report = scanner.scan_file(tmp_path)
            finally:
                tmp_path.unlink(missing_ok=True)
            return {"ok": True, **report.model_dump_report()}
        except ImportError as exc:
            return {"ok": False, "note": f"MCP scanner not available: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "note": str(exc)}

    # ---------- Adapter registry (ecosystem adapters — E-5..E-8 + v0.26.0) ----------

    KNOWN_ADAPTERS = [
        {
            "id": "langgraph",
            "module": "novafabric.adapters.langgraph",
            "function": "wrap",
            "framework": "LangGraph",
            "extra": "langgraph",
        },
        {
            "id": "autogen",
            "module": "novafabric.adapters.autogen",
            "function": "wrap_agent",
            "framework": "AutoGen / pyautogen",
            "extra": "pyautogen",
        },
        {
            "id": "crewai",
            "module": "novafabric.adapters.crewai",
            "function": "wrap_crew",
            "framework": "CrewAI",
            "extra": "crewai",
        },
        {
            "id": "dspy",
            "module": "novafabric.adapters.dspy",
            "function": "wrap_program",
            "framework": "DSPy",
            "extra": "dspy",
        },
        {
            "id": "openai_agents",
            "module": "novafabric.adapters.openai_agents",
            "function": "register",
            "framework": "OpenAI Agents SDK",
            "extra": "openai-agents",
        },
        {
            "id": "google_adk",
            "module": "novafabric.adapters.google_adk",
            "function": "make_plugin",
            "framework": "Google ADK",
            "extra": "google-adk",
        },
        {
            "id": "bedrock_agentcore",
            "module": "novafabric.adapters.bedrock_agentcore",
            "function": "wrap_client",
            "framework": "AWS Bedrock AgentCore",
            "extra": "bedrock-agentcore",
        },
        {
            "id": "a2a",
            "module": "novafabric.adapters.a2a",
            "function": "make_interceptor",
            "framework": "A2A SDK",
            "extra": "a2a",
        },
    ]

    @app.get("/api/adapters", dependencies=[Depends(verify_token)])
    async def api_adapters() -> dict[str, Any]:
        """List registered Nova framework adapters with availability flags."""
        result = []
        for adapter in KNOWN_ADAPTERS:
            spec = importlib.util.find_spec(adapter["module"])
            result.append({**adapter, "available": spec is not None})
        return {"adapters": result, "count": len(result)}

    # ---------- EU AI Act / Regulatory export endpoints (v0.34.0) ----------

    @app.get("/api/compliance/euaiact/status", dependencies=[Depends(verify_token)])
    async def euaiact_status_endpoint() -> dict[str, Any]:
        """Return EU AI Act Art.12 compliance configuration (ADR-0076)."""
        try:
            from novafabric.compliance.euaiact import EuAiActConfig  # noqa: PLC0415
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"euaiact module not available: {exc}",
            ) from exc
        try:
            cfg = EuAiActConfig()
            return {
                "ok": True,
                "high_risk": cfg.high_risk,
                "provider_mode": cfg.provider_mode,
                "retention_months": cfg.retention_months,
                "deadline": "2026-08-02",
                "note": "ADR-0076 — EU AI Act Art.12 compliance mode",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/compliance/euaiact/export", dependencies=[Depends(verify_token)])
    async def euaiact_export_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Export EU AI Act Art.12 structured log records (ADR-0076)."""
        try:
            from novafabric.compliance.euaiact import (  # noqa: PLC0415
                EuAiActConfig,
                EuAiActExporter,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"euaiact module not available: {exc}",
            ) from exc
        from datetime import datetime, timezone  # noqa: PLC0415

        cfg = EuAiActConfig()
        from_dt: datetime | None = None
        to_dt: datetime | None = None
        try:
            if body.get("from_date"):
                from_dt = datetime.fromisoformat(str(body["from_date"]).rstrip("Z")).replace(
                    tzinfo=timezone.utc
                )
            if body.get("to_date"):
                to_dt = datetime.fromisoformat(str(body["to_date"]).rstrip("Z")).replace(
                    tzinfo=timezone.utc
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=f"invalid date format: {exc}") from exc
        try:
            exporter = EuAiActExporter(capsule_dir)
            records = exporter.export(from_dt=from_dt, to_dt=to_dt)
            return {
                "ok": True,
                "records": records,
                "count": len(records),
                "retention_months": cfg.retention_months,
                "mode": "provider" if cfg.provider_mode else "deployer",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/compliance/export/rocrate", dependencies=[Depends(verify_token)])
    async def compliance_export_rocrate_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Export a capsule as W3C RO-Crate v1.1 ZIP (base64-encoded)."""
        import base64  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        run_id: str = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        try:
            from novafabric.compliance.export.ro_crate import export_ro_crate  # noqa: PLC0415
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"ro_crate module not available: {exc}",
            ) from exc
        cap_dir = _resolve_capsule(run_id, capsule_dir)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                zip_path = Path(tmp) / f"{cap_dir.name}.rocrate.zip"
                export_ro_crate(cap_dir, zip_path)
                zip_bytes = zip_path.read_bytes()
            return {
                "ok": True,
                "run_id": run_id,
                "filename": f"{cap_dir.name}.rocrate.zip",
                "zip_base64": base64.b64encode(zip_bytes).decode(),
                "size_bytes": len(zip_bytes),
                "note": "W3C RO-Crate v1.1 — decode zip_base64 to download",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/lineage/export-prov", dependencies=[Depends(verify_token)])
    async def lineage_export_prov_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Export W3C PROV-JSON lineage document for a capsule."""
        run_id: str = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        try:
            from novafabric.compliance.export.prov_json import export_prov_json  # noqa: PLC0415
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"prov_json module not available: {exc}",
            ) from exc
        cap_dir = _resolve_capsule(run_id, capsule_dir)
        try:
            doc = export_prov_json(cap_dir)
            return {
                "ok": True,
                "run_id": run_id,
                "document": doc,
                "note": "W3C PROV-JSON lineage export",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/compliance/export/c2pa", dependencies=[Depends(verify_token)])
    async def compliance_export_c2pa_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Export C2PA v2.3 manifest for a capsule (ADR-0074 / EU AI Act Art.50)."""
        import tempfile  # noqa: PLC0415

        run_id: str = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        include_training_mining: bool = bool(body.get("include_training_mining", False))
        try:
            from novafabric.evidence.c2pa_exporter import export_c2pa  # noqa: PLC0415
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"c2pa_exporter module not available: {exc}",
            ) from exc
        cap_dir = _resolve_capsule(run_id, capsule_dir)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out_path = Path(tmp) / "c2pa-manifest.json"
                export_c2pa(cap_dir, out_path, include_training_mining=include_training_mining)
                manifest = json.loads(out_path.read_text())
            return {
                "ok": True,
                "run_id": run_id,
                "manifest": manifest,
                "note": "C2PA v2.3 — ADR-0074 / EU AI Act Art.50",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ---------- GDPR RoPA export (nova export-ropa — cap-007) ----------

    @app.post("/api/compliance/export/ropa", dependencies=[Depends(verify_token)])
    async def compliance_export_ropa_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Export GDPR Art.30 Records of Processing Activities (RoPA) — nova export-ropa."""
        run_id: str = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        controller_name: str = body.get("controller_name", "")
        controller_contact: str = body.get("controller_contact", "")
        try:
            from novafabric.compliance.export.gdpr_ropa import GDPRRoPAExporter  # noqa: PLC0415
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"gdpr_ropa module not available: {exc}",
            ) from exc
        cap_dir = _resolve_capsule(run_id, capsule_dir)
        cfg: dict[str, str] = {}
        if controller_name:
            cfg["controller_name"] = controller_name
        if controller_contact:
            cfg["controller_contact"] = controller_contact
        try:
            exporter = GDPRRoPAExporter(operator_config=cfg)
            entry = exporter.build_ropa_entry(cap_dir)
            return {
                "ok": True,
                "run_id": run_id,
                "document": entry.model_dump(mode="json"),
                "completeness": entry.completeness,
                "missing_fields": entry.missing_fields,
                "note": "GDPR Art.30 Records of Processing Activities (RoPA)",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ---------- AI-SBOM export (nova export-aibom — cap-008) ----------

    @app.post("/api/compliance/export/aibom", dependencies=[Depends(verify_token)])
    async def compliance_export_aibom_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Export CycloneDX 1.7 AI-SBOM (ML-BOM) — nova export-aibom."""
        run_id: str = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        try:
            from novafabric.compliance.export.aibom import AIBOMExporter  # noqa: PLC0415
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"aibom module not available: {exc}",
            ) from exc
        cap_dir = _resolve_capsule(run_id, capsule_dir)
        try:
            exporter = AIBOMExporter()
            doc = exporter.build_aibom(cap_dir)
            return {
                "ok": True,
                "run_id": run_id,
                "bom_format": doc.bom_format,
                "serial_number": doc.serial_number,
                "component_count": len(doc.components),
                "components": [
                    {
                        "type": c.type,
                        "name": c.name,
                        "version": c.version,
                        "description": c.description,
                        "licenses": c.licenses,
                        "properties": c.properties,
                    }
                    for c in doc.components
                ],
                "generated_at": doc.generated_at.isoformat(),
                "note": "CycloneDX 1.7 AI-SBOM (ML-BOM) — cap-008 / EU CRA 2026-09-11",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ---------- NIST AI RMF export (nova export-nist-rmf — cap-009) ----------

    @app.post("/api/compliance/export/nist-rmf", dependencies=[Depends(verify_token)])
    async def compliance_export_nist_rmf_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Export NIST AI RMF 1.0 quantitative risk report — nova export-nist-rmf."""
        run_id: str = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        try:
            from novafabric.compliance.export.nist_rmf import NISTAIRMFReporter  # noqa: PLC0415
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"nist_rmf module not available: {exc}",
            ) from exc
        cap_dir = _resolve_capsule(run_id, capsule_dir)
        try:
            reporter = NISTAIRMFReporter()
            report = reporter.build_report(cap_dir)
            return {
                "ok": True,
                "run_id": run_id,
                "overall_score": report.overall_score,
                "risk_level": report.risk_level,
                "metrics": [m.model_dump(mode="json") for m in report.metrics],
                "missing_evidence": report.missing_evidence,
                "generated_at": report.generated_at.isoformat(),
                "note": "NIST AI RMF 1.0 (NIST AI 100-1) — cap-009",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ---------- PII DEK crypto-shredding (nova pii erase — ADR-0069) ----------

    @app.post("/api/compliance/pii/erase", dependencies=[Depends(verify_token)])
    async def compliance_pii_erase_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Destroy a data subject's DEK (GDPR Art.17 crypto-shredding) — nova pii erase."""
        subject_id: str = body.get("subject_id", "")
        if not subject_id:
            raise HTTPException(status_code=422, detail="subject_id is required")
        capsule_ids: list[str] = body.get("capsule_ids", []) or []
        retention_months: int = int(body.get("retention_months", 6) or 6)
        try:
            from novafabric.pii.dek.store import (  # noqa: PLC0415
                ErasureReceipt,
                open_dek_store,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"pii.dek.store module not available: {exc}",
            ) from exc
        try:
            receipt = open_dek_store().erase_subject(
                subject_id, capsule_ids, retention_months
            )
            erased = isinstance(receipt, ErasureReceipt)
            note = (
                "DEK destroyed; ciphertext is permanently unrecoverable (GDPR Art.17)."
                if erased
                else (
                    f"Erasure deferred — within {retention_months}-month Art.17(3)(b) "
                    "retention window. Retry after earliest_erasure_at."
                )
            )
            return {
                "ok": True,
                "subject_id": subject_id,
                "erased": erased,
                "receipt": receipt.model_dump(mode="json"),
                "note": note,
            }
        except KeyError:
            return {
                "ok": False,
                "subject_id": subject_id,
                "error": "no DEK found",
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ---------- HIPAA Safe Harbor proof (nova export-hipaa-proof) ----------

    @app.post("/api/compliance/export/hipaa-proof", dependencies=[Depends(verify_token)])
    async def compliance_export_hipaa_proof_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Export HIPAA Safe Harbor de-identification proof — nova export-hipaa-proof."""
        run_id: str = body.get("run_id", "")
        if not run_id:
            raise HTTPException(status_code=422, detail="run_id is required")
        try:
            from novafabric.compliance.export.hipaa_safe_harbor import (  # noqa: PLC0415
                HIPAASafeHarborExporter,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=501,
                detail=f"hipaa_safe_harbor module not available: {exc}",
            ) from exc
        cap_dir = _resolve_capsule(run_id, capsule_dir)
        try:
            proof = HIPAASafeHarborExporter().build_proof(cap_dir)
            return {
                "ok": True,
                "run_id": run_id,
                "proof_digest": proof.proof_digest,
                "disclaimer": proof.legal_disclaimer,
                "assessed_at": proof.assessment_date,
                "identifier_count": len(proof.identifiers),
                "categories": [
                    {
                        "id": a.identifier,
                        "name": a.label,
                        "status": a.status,
                        "evidence_source": a.redaction_method,
                    }
                    for a in proof.identifiers
                ],
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    # ---------- AIBOM status (nova aibom status — cap-008) ----------

    @app.get("/api/aibom/status", dependencies=[Depends(verify_token)])
    async def aibom_status_endpoint() -> dict[str, Any]:
        """Show CRA SBOM compliance status — nova aibom status."""
        _CRA_DEADLINE = "2026-09-11"
        caps_dir = capsule_dir
        total = 0
        covered = 0
        if caps_dir.is_dir():
            for entry in caps_dir.iterdir():
                if not entry.is_dir():
                    continue
                if not (entry / "capsule.yaml").exists():
                    continue
                total += 1
                if (entry / "aibom.json").exists():
                    covered += 1
        missing = total - covered
        if total == 0:
            coverage_status = "no_capsules"
        elif covered == total:
            coverage_status = "complete"
        else:
            coverage_status = "partial"
        return {
            "ok": True,
            "regulation": "EU Cyber Resilience Act (Regulation 2024/2847)",
            "cra_deadline": _CRA_DEADLINE,
            "spec_version": "CycloneDX ML-BOM 1.7 (ECMA-424 2nd Edition)",
            "capsule_directory": str(caps_dir),
            "total_capsules": total,
            "capsules_with_aibom": covered,
            "capsules_missing_aibom": missing,
            "coverage_status": coverage_status,
            "note": "nova aibom status — CRA SBOM compliance coverage",
        }

    # ---------- v0.46.0 dashboard parity gap closure ----------
    # 12 CLI capabilities that previously had no dashboard equivalent.

    @app.get("/api/eval/suites", dependencies=[Depends(verify_token)])
    async def eval_suites_endpoint() -> dict[str, Any]:
        """List registered eval suite adapters — nova eval list."""
        ep_group = "novafabric.eval_suites"
        suites: list[dict[str, Any]] = []
        for ep in sorted(importlib_metadata.entry_points(group=ep_group), key=lambda e: e.name):
            try:
                raw = ep.load()
                instance = raw() if callable(raw) else raw
                suites.append(
                    {
                        "suite_id": instance.suite_id(),
                        "version": instance.version(),
                        "oci_digest": instance.oci_digest() or None,
                        "entry_point": ep.value,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                suites.append(
                    {
                        "suite_id": ep.name,
                        "version": None,
                        "oci_digest": None,
                        "entry_point": ep.value,
                        "error": str(exc),
                    }
                )
        return {"ok": True, "entry_point_group": ep_group, "suites": suites}

    @app.post("/api/eval/run", dependencies=[Depends(verify_token)])
    async def eval_run_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Run a standard eval suite against a capsule — nova eval run."""
        from novafabric.evals._loader import load_eval_suite  # noqa: PLC0415
        from novafabric.evals.exceptions import EvalSuiteError  # noqa: PLC0415

        run_id: str = body.get("run_id", "")
        suite: str = body.get("suite", "")
        if not run_id or not suite:
            raise HTTPException(status_code=422, detail="run_id and suite are required")
        config: dict[str, str] = {
            str(k): str(v) for k, v in (body.get("config") or {}).items()
        }
        cdir = _resolve_capsule(run_id, capsule_dir)
        try:
            adapter = load_eval_suite(suite)
        except EvalSuiteError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            result = adapter.run(cdir, config)
        except EvalSuiteError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "result": result.model_dump(mode="json")}

    @app.get("/api/policy/list", dependencies=[Depends(verify_token)])
    async def policy_list_endpoint(
        namespace: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List Rego bundle files and signed promotion policies — nova policy list."""
        from novafabric._paths import nova_home  # noqa: PLC0415
        from novafabric.policy._opa_engine import _get_bundle_default  # noqa: PLC0415
        from novafabric.promote.policy_store import PolicyStore  # noqa: PLC0415

        bundle_path = _get_bundle_default()
        rego_files: list[dict[str, Any]] = []
        if bundle_path.exists():
            for f in sorted(bundle_path.rglob("*.rego")):
                rego_files.append(
                    {
                        "file": str(f.relative_to(bundle_path)),
                        "size_bytes": f.stat().st_size,
                    }
                )

        home_db = nova_home() / "promote" / "policy.db"
        legacy_db = Path.home() / ".local" / "share" / "novafabric" / "merkle.db"
        policy_db = home_db if home_db.exists() else legacy_db
        signed_policies: list[dict[str, Any]] = []
        if policy_db.exists():
            try:
                store = PolicyStore(policy_db)
                signed_policies = [
                    {
                        "version": row["version"],
                        "namespace": row["namespace"],
                        "created_at": row["created_at"],
                    }
                    for row in store.list_all(namespace=namespace)
                ]
                store.close()
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "ok": True,
            "bundle_path": str(bundle_path),
            "rego_files": rego_files,
            "policy_db": str(policy_db),
            "signed_policies": signed_policies,
        }

    @app.post("/api/policy/sign", dependencies=[Depends(verify_token)])
    async def policy_sign_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Sign and store a new promotion policy — nova policy sign."""
        from novafabric.promote.exceptions import (  # noqa: PLC0415
            PredicateValidationError,
        )
        from novafabric.promote.policy_store import PolicyStore  # noqa: PLC0415
        from novafabric.promote.predicates import (  # noqa: PLC0415
            POLICY_PAYLOAD_TYPE,
            build_policy_predicate,
            sign_promote_envelope,
            validate_predicate,
        )

        key_path = Path(str(body.get("key_path", "")))
        cert_path = Path(str(body.get("cert_path", "")))
        proposers = [str(s) for s in (body.get("proposer_subjects") or []) if str(s).strip()]
        approvers = [str(s) for s in (body.get("approver_subjects") or []) if str(s).strip()]
        bypass_valid_hours = int(body.get("bypass_valid_hours", 24) or 24)
        db_raw = body.get("db_path")
        db_path_policy = (
            Path(str(db_raw))
            if db_raw
            else Path.home() / ".local" / "share" / "novafabric" / "merkle.db"
        )

        if not proposers:
            raise HTTPException(status_code=400, detail="proposer_subjects must not be empty")
        if not approvers:
            raise HTTPException(status_code=400, detail="approver_subjects must not be empty")
        if not (1 <= bypass_valid_hours <= 168):
            raise HTTPException(
                status_code=400,
                detail=f"bypass_valid_hours must be between 1 and 168 (got {bypass_valid_hours})",
            )
        if not key_path.exists():
            raise HTTPException(status_code=400, detail=f"key file not found: {key_path}")
        if not cert_path.exists():
            raise HTTPException(status_code=400, detail=f"cert file not found: {cert_path}")

        predicate = build_policy_predicate(proposers, approvers, bypass_valid_hours)
        try:
            validate_predicate("promote_policy_v1.json", predicate)
        except PredicateValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        payload = json.dumps(predicate).encode()
        try:
            envelope = sign_promote_envelope(
                payload, POLICY_PAYLOAD_TYPE, key_path, cert_path
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"signing error: {exc}") from exc
        store = PolicyStore(db_path_policy)
        version = store.put(envelope.decode("utf-8"))
        store.close()
        return {
            "ok": True,
            "version": version,
            "proposers": proposers,
            "approvers": approvers,
            "bypass_valid_hours": bypass_valid_hours,
            "policy_db": str(db_path_policy),
        }

    @app.get("/api/governance/vocabularies", dependencies=[Depends(verify_token)])
    async def governance_vocabularies_endpoint() -> dict[str, Any]:
        """List vocabulary versions — nova classify list-vocabularies."""
        import yaml as _yaml  # noqa: PLC0415

        from novafabric.governance.classifier import (  # noqa: PLC0415
            _DEFAULT_VOCAB_DIR,
        )

        vocabularies: list[dict[str, Any]] = []
        if _DEFAULT_VOCAB_DIR.is_dir():
            for framework_dir in sorted(_DEFAULT_VOCAB_DIR.iterdir()):
                if not framework_dir.is_dir():
                    continue
                for vocab_file in sorted(framework_dir.glob("*.yaml")):
                    try:
                        meta = _yaml.safe_load(vocab_file.read_text())
                        version = meta.get("version", "?")
                        reference = meta.get("reference", meta.get("regulation", ""))
                    except Exception:  # noqa: BLE001
                        version = "?"
                        reference = ""
                    vocabularies.append(
                        {
                            "framework": framework_dir.name,
                            "version": version,
                            "reference": reference,
                            "path": str(vocab_file.relative_to(_DEFAULT_VOCAB_DIR.parent)),
                        }
                    )
        return {"ok": True, "vocabularies": vocabularies}

    @app.post("/api/governance/classify-manual", dependencies=[Depends(verify_token)])
    async def governance_classify_manual_endpoint(
        body: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """Classify a manually described AI system — nova classify run."""
        from novafabric.governance import (  # noqa: PLC0415
            AISystemRecord,
            RiskTierClassifier,
        )

        for field in ("name", "description", "use_case_domain", "deployment_context"):
            if not str(body.get(field, "")).strip():
                raise HTTPException(status_code=422, detail=f"{field} is required")
        record = AISystemRecord(
            name=str(body["name"]),
            description=str(body["description"]),
            use_case_domain=str(body["use_case_domain"]),
            deployment_context=str(body["deployment_context"]),
            uses_biometrics=bool(body.get("uses_biometrics", False)),
            affects_fundamental_rights=bool(body.get("affects_fundamental_rights", False)),
            is_general_purpose=bool(body.get("is_general_purpose", False)),
        )
        try:
            result = RiskTierClassifier().classify(record)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "classification": result.model_dump(mode="json")}

    @app.post("/api/aibom/generate", dependencies=[Depends(verify_token)])
    async def aibom_generate_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Generate CycloneDX AI-BOM(s) for one or all capsules — nova aibom generate."""
        from novafabric.compliance.export.aibom import AIBOMExporter  # noqa: PLC0415

        run_id = body.get("run_id") or None
        all_capsules = bool(body.get("all", False))
        force = bool(body.get("force", False))
        if not all_capsules and not run_id:
            raise HTTPException(status_code=400, detail="provide run_id or all=true")

        exporter = AIBOMExporter()

        def _generate_one(cap: Path) -> bool:
            out_path = cap / "aibom.json"
            if out_path.exists() and not force:
                return False
            doc = exporter.build_aibom(cap)
            exporter.export_json(doc, out_path)
            return True

        if all_capsules:
            written = 0
            skipped = 0
            failed = 0
            errors: list[str] = []
            if capsule_dir.is_dir():
                for entry in sorted(capsule_dir.iterdir()):
                    if not entry.is_dir() or not (entry / "capsule.yaml").exists():
                        continue
                    try:
                        if _generate_one(entry):
                            written += 1
                        else:
                            skipped += 1
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        errors.append(f"{entry.name}: {exc}")
            return {
                "ok": failed == 0,
                "written": written,
                "skipped": skipped,
                "failed": failed,
                "errors": errors,
            }

        cdir = _resolve_capsule(str(run_id), capsule_dir)
        try:
            wrote = _generate_one(cdir)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "ok": True,
            "written": 1 if wrote else 0,
            "skipped": 0 if wrote else 1,
            "failed": 0,
            "path": str(cdir / "aibom.json"),
        }

    @app.post("/api/ingest-capsule", dependencies=[Depends(verify_token)])
    async def ingest_capsule_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Index capsule(s) into the runs metadata store — nova ingest-capsule."""
        from novafabric.serve.capsule_watcher import CapsuleWatcher  # noqa: PLC0415

        run_id = body.get("run_id") or None
        all_capsules = bool(body.get("all", False))
        if not all_capsules and not run_id:
            raise HTTPException(status_code=400, detail="provide run_id or all=true")
        watcher = CapsuleWatcher(capsule_dir, db_path=_db_path)
        if all_capsules:
            indexed = watcher.ingest_all()
            return {"ok": True, "indexed": indexed}
        found, is_new = watcher.ingest_one(str(run_id))
        if not found:
            raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
        return {"ok": True, "found": True, "is_new": is_new}

    @app.get("/api/runs/{run_id}/tree", dependencies=[Depends(verify_token)])
    async def run_tree_endpoint(run_id: str) -> dict[str, Any]:
        """Show the parent/child capsule tree — nova run show --with-children."""
        from novafabric.capsule.tree_assembler import (  # noqa: PLC0415
            CapsuleNode,
            CapsuleTreeAssembler,
        )

        if "/" in run_id or ".." in run_id:
            raise HTTPException(status_code=400, detail="invalid run_id")

        def _node_dict(n: CapsuleNode) -> dict[str, Any]:
            return {
                "run_id": n.run_id,
                "status": n.status,
                "capsule_role": n.capsule_role,
                "is_synthetic": n.is_synthetic,
                "children": [_node_dict(c) for c in n.children],
            }

        try:
            tree = CapsuleTreeAssembler(capsule_dir).assemble_tree(run_id)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {
            "ok": True,
            "root": _node_dict(tree.root),
            "total_nodes": tree.total_nodes,
            "orphans": [_node_dict(o) for o in tree.orphan_children],
        }

    @app.get("/api/runs/{run_id}/run-lineage", dependencies=[Depends(verify_token)])
    async def run_lineage_endpoint(
        run_id: str,
        edge_types: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List spool lineage edges for a distributed run — nova run lineage."""
        from novafabric.capsule.schema import EdgeType  # noqa: PLC0415

        if "/" in run_id or ".." in run_id:
            raise HTTPException(status_code=400, detail="invalid run_id")

        filter_types: set[str] | None = None
        if edge_types:
            raw_types = [t.strip() for t in edge_types.split(",") if t.strip()]
            valid_values = {e.value for e in EdgeType}
            invalid = [t for t in raw_types if t not in valid_values]
            if invalid:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"unknown edge types: {', '.join(invalid)}; "
                        f"valid: {', '.join(sorted(valid_values))}"
                    ),
                )
            filter_types = set(raw_types)

        edges: list[dict[str, Any]] = []
        if capsule_dir.is_dir():
            for entry in capsule_dir.iterdir():
                if not entry.is_dir():
                    continue
                lineage_path = entry / "lineage.jsonl"
                if not lineage_path.exists():
                    continue
                for line in lineage_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    src = record.get("source_run_id", "")
                    tgt = record.get("target_run_id", "")
                    if run_id not in (src, tgt):
                        continue
                    et = record.get("edge_type", "contains")
                    if filter_types and et not in filter_types:
                        continue
                    edges.append(record)
        return {
            "ok": True,
            "run_id": run_id,
            "edge_types_filter": sorted(filter_types) if filter_types else None,
            "edges": edges,
            "count": len(edges),
        }

    @app.get("/api/lineage-store/profile", dependencies=[Depends(verify_token)])
    async def lineage_store_profile_endpoint(
        target: str = Query(default="kuzudb-vertical"),
        node_size: str = Query(default="16g-ram-500g-nvme"),
        rf: int = Query(default=3),
        image_tag: str = Query(default="latest"),
    ) -> dict[str, Any]:
        """Generate a lineage-store deployment profile — nova lineage-store profile."""
        if target == "kuzudb-vertical":
            from novafabric.lineage.profiles.kuzudb_vertical import (  # noqa: PLC0415
                generate_kuzudb_vertical_profile,
            )

            profile_yaml = generate_kuzudb_vertical_profile(
                node_size=node_size, image_tag=image_tag
            )
        elif target == "janusgraph-minimal":
            from novafabric.lineage.profiles.janusgraph_minimal import (  # noqa: PLC0415
                generate_janusgraph_minimal_profile,
            )

            profile_yaml = generate_janusgraph_minimal_profile(
                replication_factor=rf, image_tag=image_tag
            )
        else:
            raise HTTPException(
                status_code=400,
                detail=f"unknown target {target!r}; choose: kuzudb-vertical | janusgraph-minimal",
            )
        return {"ok": True, "target": target, "profile_yaml": profile_yaml}

    @app.get("/api/runs/{run_id}/scan-secrets", dependencies=[Depends(verify_token)])
    async def scan_secrets_endpoint(
        run_id: str,
        fail_on: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Report secret/PII findings from the redaction log — nova scan-secrets."""
        severity_order = ["critical", "high", "medium", "low", "info"]
        if fail_on is not None and fail_on not in severity_order:
            raise HTTPException(
                status_code=400,
                detail=f"invalid fail_on {fail_on!r}; expected one of {severity_order}",
            )
        cdir = _resolve_capsule(run_id, capsule_dir)
        proof_path = cdir / "redaction-proof.json"
        if not proof_path.exists():
            raise HTTPException(
                status_code=404,
                detail="missing redaction-proof.json — run `nova redact` first",
            )
        try:
            proof = json.loads(proof_path.read_text())
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=500, detail=f"corrupt proof: {exc}") from exc
        findings = proof.get("findings", [])
        triggered_count = 0
        if fail_on is not None:
            threshold_idx = severity_order.index(fail_on)
            # An unrecognized severity ranks lowest (never triggers) rather than
            # raising ValueError → 500 on a malformed proof entry.
            lowest = len(severity_order) - 1

            def _rank(sev: str) -> int:
                return severity_order.index(sev) if sev in severity_order else lowest

            triggered_count = sum(
                1 for f in findings if _rank(f.get("severity", "info")) <= threshold_idx
            )
        return {
            "ok": True,
            "run_id": run_id,
            "findings_count": proof.get("findings_count", {}),
            "findings": findings,
            "fail_on": fail_on,
            "triggered": triggered_count > 0,
            "triggered_count": triggered_count,
        }

    # ---------- Topology Dashboard (--topology flag) ----------
    # MUST be registered before the StaticFiles catch-all mount below.
    # Starlette matches routes in insertion order; a Mount("/") prefix-matches
    # everything, so any routes added after it are unreachable.

    if topology_enabled:
        import asyncio as _asyncio
        import json as _json_topo

        from starlette.responses import Response as _StarletteResponse
        from starlette.websockets import WebSocketState as _WebSocketState

        from novafabric.serve.topology.ads_encoder import (
            encode_delta_event_arrow as _encode_delta_arrow,
        )
        from novafabric.serve.topology.cluster_store import ClusterStore as _ClusterStore
        from novafabric.serve.topology.delta_buffer import DeltaBuffer as _DeltaBuffer
        from novafabric.serve.topology.topology_extractor import (
            TopologyExtractor as _TopologyExtractor,
        )

        # Louvain resolution: explicit arg > env var > python-louvain default (1.0).
        # Lower values merge into fewer/larger clusters; higher values split further.
        if topology_louvain_resolution is not None:
            _louvain_res = topology_louvain_resolution
        else:
            try:
                _louvain_res = float(
                    os.environ.get("NOVA_TOPOLOGY_LOUVAIN_RESOLUTION", "1.0")
                )
            except ValueError:
                _louvain_res = 1.0

        _topo_store = _ClusterStore()
        _topo_delta = _DeltaBuffer()
        _topo_extractor = _TopologyExtractor(
            _topo_store, _topo_delta, louvain_resolution=_louvain_res
        )
        _topo_ws_clients: list[_asyncio.Queue] = []  # type: ignore[type-arg]

        # Tracks which capsule directories have already been seeded into the
        # topology store.  Keyed by the resolved string path of each capsule
        # directory.  Used by _topology_auto_reseed_loop to avoid duplicate edges.
        _topology_seeded_dirs: set[str] = set()

        async def _topology_seed_all(
            *,
            only_dirs: set[str] | None = None,
            tv5_pipe: Any = None,
        ) -> dict[str, Any]:
            """Seed the topology store from capsules on disk.

            If *only_dirs* is given (a set of resolved-path strings), only those
            directories are processed.  Otherwise all capsule dirs are scanned.
            Newly seeded dirs are added to ``_topology_seeded_dirs``.
            Returns a summary dict compatible with the /api/topology/seed response.
            """
            import json as _jseed
            import time as _time

            agents_added = 0
            models_added: set[str] = set()
            edges_added = 0
            errors: list[str] = []

            for cap_dir in sorted(capsule_dir.iterdir()):
                if not cap_dir.is_dir():
                    continue
                manifest_path = cap_dir / "capsule.yaml"
                if not manifest_path.exists():
                    continue
                resolved_key = str(cap_dir.resolve())
                if only_dirs is not None and resolved_key not in only_dirs:
                    continue
                run_id = cap_dir.name
                try:
                    import yaml as _yaml

                    manifest = _yaml.safe_load(manifest_path.read_text())
                    status = "success" if manifest.get("exit_code", 1) == 0 else "failed"
                except Exception:  # noqa: BLE001
                    status = "idle"

                agent_id = f"run:{run_id}"
                _topo_extractor.add_agent(agent_id, agent_type="run", status=status)
                agents_added += 1

                mc_path = cap_dir / "model-calls.jsonl"
                if mc_path.exists():
                    for line in mc_path.read_text().splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            mc = _jseed.loads(line)
                        except Exception:  # noqa: BLE001
                            continue
                        model = (
                            mc.get("gen_ai.response.model") or mc.get("gen_ai.request.model", "")
                        )
                        provider = mc.get("gen_ai.system", "unknown")
                        if not model:
                            continue
                        model_id = f"model:{provider}/{model}"
                        if model_id not in models_added:
                            _topo_extractor.add_agent(model_id, agent_type="model", status="idle")
                            models_added.add(model_id)
                        _topo_extractor.add_edge(agent_id, model_id, edge_type="calls")
                        edges_added += 1

                _topology_seeded_dirs.add(resolved_key)

            await _topo_extractor.run_louvain_pass()

            # If TV-5 router is active, compute a 3D snapshot from the same graph.
            tv5_window_id: str | None = None
            if tv5_pipe is not None:
                window_id = f"seed_{int(_time.time())}"
                try:
                    await tv5_pipe.compute_snapshot(
                        _topo_extractor.get_edges(),
                        _topo_extractor.get_node_types(),
                        window_id,
                    )
                    tv5_window_id = window_id
                except Exception as _e:  # noqa: BLE001
                    errors.append(f"tv5_snapshot failed: {_e}")

            result: dict[str, Any] = {
                "ok": True,
                "agents_added": agents_added,
                "models_added": len(models_added),
                "edges_added": edges_added,
                "errors": errors,
                "node_count": _topo_extractor.node_count(),
                "edge_count": _topo_extractor.edge_count(),
                "cluster_count": _topo_store.cluster_count(),
            }
            if tv5_window_id is not None:
                result["tv5_window_id"] = tv5_window_id
            return result

        async def _topology_auto_reseed_loop(interval_seconds: float = 60.0) -> None:
            """Background asyncio task: re-seed topology when new capsules appear.

            On each tick, scans *capsule_dir* for directories that are not yet
            in ``_topology_seeded_dirs``.  Only newly discovered directories are
            passed to ``_topology_seed_all`` to avoid duplicating edges.
            """
            while True:
                await asyncio.sleep(interval_seconds)
                try:
                    new_dirs: set[str] = set()
                    for cap_dir in capsule_dir.iterdir():
                        if not cap_dir.is_dir():
                            continue
                        if not (cap_dir / "capsule.yaml").exists():
                            continue
                        key = str(cap_dir.resolve())
                        if key not in _topology_seeded_dirs:
                            new_dirs.add(key)
                    if not new_dirs:
                        continue
                    result = await _topology_seed_all(only_dirs=new_dirs)
                    logger.info(
                        "topology: auto-reseed added %d agent(s), %d edge(s) "
                        "from %d new capsule dir(s)",
                        result["agents_added"],
                        result["edges_added"],
                        len(new_dirs),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("topology: auto-reseed tick error: %s", exc)

        # Wire topology hooks so _lifespan can call them at startup / shutdown.
        _topo_hooks["seed_fn"] = _topology_seed_all
        _topo_hooks["loop_fn"] = _topology_auto_reseed_loop

        @app.get("/topology/clusters", dependencies=[Depends(verify_token)])
        async def topology_clusters_endpoint() -> _StarletteResponse:
            """Return Arrow IPC cluster layer (ADS v1 cluster_layer schema)."""
            ipc = _topo_store.fetch_cluster_layer_arrow()
            return _StarletteResponse(
                content=ipc,
                media_type="application/vnd.apache.arrow.stream",
            )

        @app.get("/topology/cluster-edges", dependencies=[Depends(verify_token)])
        async def topology_cluster_edges_endpoint() -> list[dict[str, Any]]:
            """Return inter-cluster edge aggregates for drawing edges between super-nodes."""
            return _topo_store.fetch_inter_cluster_edges()

        @app.get("/topology/cluster-list", dependencies=[Depends(verify_token)])
        async def topology_cluster_list_endpoint() -> list[dict[str, Any]]:
            """Return clusters as plain JSON rows (largest first) for the Table/Treemap views."""
            return _topo_store.list_clusters()

        @app.websocket("/topology/stream")
        async def topology_stream_ws(websocket: WebSocket) -> None:
            """TDP v1 WebSocket endpoint for topology delta events (FR-05..FR-09).

            A-1: live delta push — DeltaBuffer.subscribe() registers a callback
            that feeds events directly into this connection's asyncio Queue so
            clients receive add_node/add_edge/etc. within <1 s of enqueue.

            A-2: delta events are encoded as binary Arrow IPC frames via
            encode_delta_event_arrow() instead of send_text(JSON), matching the
            ADR-002 binary transport spec.  subgraph_expand frames remain binary
            Arrow IPC (unchanged).  The heartbeat batch_checkpoint is also binary.
            """
            proto = websocket.headers.get("sec-websocket-protocol", "")
            if "nova-tdp-v1" not in proto:
                await websocket.close(code=4400)
                return

            # WebSocket scope bypasses the HTTP host-guard middleware and the
            # verify_token HTTP dependency, so enforce both inline before accept:
            # token auth + the DNS-rebinding localhost host check.
            if not is_localhost_host(websocket.headers.get("host")):
                await websocket.close(code=4403)
                return
            ws_token = websocket.query_params.get("token")
            if not ws_token or not _consteq(ws_token, token):
                await websocket.close(code=4401)
                return

            await websocket.accept(subprotocol="nova-tdp-v1")

            # Per-connection asyncio Queue for outbound binary frames.
            # Each item is a bytes object (Arrow IPC frame).
            queue: _asyncio.Queue[bytes] = _asyncio.Queue(maxsize=1000)

            loop = _asyncio.get_event_loop()

            def _on_delta_event(event: dict[str, Any]) -> None:
                """Called synchronously by DeltaBuffer.enqueue() under the buffer lock.

                Must be non-blocking — schedule the IPC encode + queue put on the
                event loop thread so Arrow encoding happens off the lock path.
                """
                try:
                    ipc_bytes = _encode_delta_arrow(event)
                    loop.call_soon_threadsafe(queue.put_nowait, ipc_bytes)
                except Exception:
                    pass

            sub_id = _topo_delta.subscribe(_on_delta_event)
            _topo_ws_clients.append(queue)

            async def _heartbeat() -> None:
                while True:
                    await _asyncio.sleep(10)
                    ckpt = _topo_delta.checkpoint()
                    try:
                        # checkpoint() calls enqueue() which triggers _on_delta_event;
                        # the heartbeat event is therefore already in the queue via
                        # pub-sub.  We still call checkpoint() here so the seq number
                        # advances on schedule, but no additional put is needed.
                        _ = ckpt
                    except Exception:
                        pass

            async def _sender() -> None:
                while True:
                    ipc_bytes = await queue.get()
                    if websocket.client_state == _WebSocketState.CONNECTED:
                        await websocket.send_bytes(ipc_bytes)

            heartbeat_task = _asyncio.create_task(_heartbeat())
            sender_task = _asyncio.create_task(_sender())
            try:
                async for msg in websocket.iter_text():
                    try:
                        req = _json_topo.loads(msg)
                    except Exception:
                        continue
                    rtype = req.get("type")
                    if rtype == "subgraph_expand":
                        cluster_id = int(req.get("cluster_id", -1))
                        nodes_ipc, edges_ipc = _topo_store.fetch_subgraph_arrow(cluster_id)
                        await websocket.send_bytes(nodes_ipc)
                        await websocket.send_bytes(edges_ipc)
                        ckpt_ipc = _encode_delta_arrow(_topo_delta.checkpoint())
                        await websocket.send_bytes(ckpt_ipc)
                    elif rtype == "subgraph_collapse":
                        ckpt_ipc = _encode_delta_arrow(_topo_delta.checkpoint())
                        await websocket.send_bytes(ckpt_ipc)
                    elif rtype == "resume_from":
                        checkpoint_id = int(req.get("checkpoint_id", 0))
                        for ev in _topo_delta.replay_since(checkpoint_id):
                            await websocket.send_bytes(_encode_delta_arrow(ev))
            except Exception:
                pass
            finally:
                heartbeat_task.cancel()
                sender_task.cancel()
                _topo_delta.unsubscribe(sub_id)
                if queue in _topo_ws_clients:
                    _topo_ws_clients.remove(queue)

        @app.get("/metrics/stream", dependencies=[Depends(verify_token)])
        async def metrics_sse_stream(request: Request) -> _StarletteResponse:
            """SSE metrics stream (FR-21: Last-Event-ID reconnect support)."""
            import time as _t

            last_event_id_hdr = request.headers.get("last-event-id", "0")
            try:
                _event_id = int(last_event_id_hdr) + 1
            except ValueError:
                _event_id = 1

            async def _generate():  # type: ignore[no-untyped-def]
                nonlocal _event_id
                while True:
                    if await request.is_disconnected():
                        break
                    summary = {
                        "ts_ms": int(_t.time() * 1000),
                        "node_count": _topo_extractor.node_count(),
                        "edge_count": _topo_extractor.edge_count(),
                        "cluster_count": _topo_store.cluster_count(),
                    }
                    yield (
                        f"id: {_event_id}\n"
                        f"data: {_json_topo.dumps(summary)}\n\n"
                    )
                    _event_id += 1
                    # Sleep in short intervals so disconnect is detected quickly.
                    for _ in range(50):
                        await _asyncio.sleep(0.1)
                        if await request.is_disconnected():
                            return

            return StreamingResponse(
                content=_generate(),  # type: ignore[no-untyped-call]
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.post("/api/topology/seed", dependencies=[Depends(verify_token)])
        async def topology_seed_endpoint(request: Request) -> dict[str, Any]:
            """Seed the live topology store from capsules already on disk.

            Delegates to ``_topology_seed_all()``.  Idempotent: re-running
            merges; duplicate node IDs are no-ops in ClusterStore.

            If the server was started with --tv5, also computes a 3D layout
            snapshot so the TV-5 view is populated immediately.
            """
            tv5_pipe = getattr(request.app.state, "tv5_layout_pipe", None)
            return await _topology_seed_all(tv5_pipe=tv5_pipe)

        @app.get("/api/topology/snapshot", dependencies=[Depends(verify_token)])
        async def topology_snapshot_endpoint() -> dict[str, Any]:
            """Return current topology counts for the SPA status bar."""
            return {
                "node_count": _topo_extractor.node_count(),
                "edge_count": _topo_extractor.edge_count(),
                "cluster_count": _topo_store.cluster_count(),
            }

    # ---------- Registry utilities: validate-spec, asset report ----------

    @app.post("/api/validate-spec", dependencies=[Depends(verify_token)])
    async def validate_spec_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Validate an asset YAML spec without registering — mirrors `nova validate <spec>`."""
        import tempfile  # noqa: PLC0415

        spec_yaml: str = body.get("spec_yaml", "")
        if not spec_yaml.strip():
            raise HTTPException(status_code=422, detail="spec_yaml is required")
        try:
            from novafabric.spec.validator import (  # noqa: PLC0415
                SpecValidationError,
                validate_spec,
            )

            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
                tmp.write(spec_yaml)
                tmp_path = Path(tmp.name)
            try:
                validate_spec(tmp_path)
                return {"ok": True, "valid": True, "errors": [], "note": "Spec is valid"}
            except SpecValidationError as exc:
                return {
                    "ok": True,
                    "valid": False,
                    "errors": [str(exc)],
                    "note": "Spec has validation errors",
                }
            finally:
                tmp_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "valid": False,
                "errors": [str(exc)],
                "note": "Parse or validation error",
            }

    @app.get("/api/report", dependencies=[Depends(verify_token)])
    async def asset_report_endpoint(format_: str = Query("json", alias="format")) -> dict[str, Any]:
        """Generate an asset inventory report — mirrors `nova report`."""
        try:
            from novafabric.report.generator import generate_report  # noqa: PLC0415

            content = generate_report(format_, db_path=db_path)
            return {
                "ok": True,
                "format": format_,
                "content": content,
                "note": f"Report generated ({format_})",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "format": format_, "content": "", "note": str(exc)}

    # ---------- v0.27.0: admin completions ----------
    # assign/revoke-role UI, flush-jwks, db-upgrade, capsule-migrate

    @app.post("/api/admin/flush-jwks-cache", dependencies=[Depends(verify_token)])
    async def flush_jwks_cache_endpoint() -> dict[str, Any]:
        """Flush the JWKS cache, forcing a re-fetch from the OIDC provider."""
        try:
            from novafabric.server.auth import flush_jwks_cache  # noqa: PLC0415

            flush_jwks_cache()
            return {
                "ok": True,
                "note": "JWKS cache cleared. Next token validation will re-fetch from provider.",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "note": f"Cache clear attempted (server may not be running): {exc}",
            }

    @app.post("/api/db/upgrade", dependencies=[Depends(verify_token)])
    async def db_upgrade_endpoint(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Run alembic upgrade to the specified revision (default: head)."""
        revision: str = body.get("revision", "head")
        try:
            from novafabric.db.migrations import (  # type: ignore[import-untyped]  # noqa: PLC0415
                run_alembic_upgrade,
            )

            result = run_alembic_upgrade(revision=revision)
            return {
                "ok": True,
                "revision": revision,
                "output": result,
                "note": f"Upgraded to {revision}",
            }
        except ImportError:
            return {
                "ok": False,
                "revision": revision,
                "note": (
                    "Alembic migrations not available in local mode"
                    " (requires novafabric server install)"
                ),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "revision": revision, "note": str(exc)}

    @app.post("/api/capsule-migrate", dependencies=[Depends(verify_token)])
    async def capsule_migrate_endpoint(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Migrate a v0.1.x capsule directory to v1.0.0 format (ADR-0034 §6)."""
        source: str = body.get("source", "")
        output: str = body.get("output", "")
        if not source:
            raise HTTPException(status_code=422, detail="source is required")
        if not output:
            raise HTTPException(status_code=422, detail="output is required")
        source_path = Path(source)
        output_path = Path(output)
        if not source_path.exists():
            raise HTTPException(status_code=404, detail=f"Source not found: {source}")
        try:
            from novafabric.capsule_migration._converter import CapsuleMigrator  # noqa: PLC0415

            result = CapsuleMigrator().migrate(source_path, output_path)
            return {
                "ok": True,
                "source": source,
                "output": output,
                "files_migrated": len(result.files_updated),
                "files_updated": result.files_updated,
                "lineage_edge_id": result.lineage_edge_id,
                "note": "Migration complete",
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "source": source, "output": output, "note": str(exc)}

    # ==================================================================
    # Dashboard CLI-coverage expansion (incidents, diagnose, evidence
    # assertions, seal ratchet, daemon status, system card, rebuild).
    # Each route calls the underlying service/domain functions directly —
    # never the Typer CLI layer. "Safe mutations only": destructive /
    # process-control routes require confirmed=True.
    # ==================================================================

    def _dump(obj: Any) -> Any:
        """Best-effort JSON-able dump for pydantic models or dataclasses."""
        import dataclasses

        if hasattr(obj, "model_dump"):
            return obj.model_dump(mode="json")
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
        return obj

    # ---------- Incidents — Art. 73 deadline clock (nova incident) ----------

    def _incident_view(inc: Any) -> dict[str, Any]:
        """Serialise an incident plus its nearest Art. 73 deadline summary."""
        from datetime import datetime, timezone

        from novafabric.compliance.incident.clock import DeadlineClock

        now = datetime.now(timezone.utc)
        deadlines = DeadlineClock.compute(inc, now=now)
        nearest = deadlines[0] if deadlines else None
        view: dict[str, Any] = _dump(inc)
        view["deadlines"] = [_dump(d) for d in deadlines]
        if nearest is not None:
            secs = (nearest.deadline - now).total_seconds()
            view["nearest_deadline"] = nearest.deadline.isoformat()
            view["nearest_obligation"] = nearest.obligation.value
            view["hours_remaining"] = round(secs / 3600, 1)
            view["overdue"] = nearest.overdue
        return view

    @app.get("/api/incidents", dependencies=[Depends(verify_token)])
    async def api_incidents_list() -> dict[str, Any]:
        """List all incidents with their nearest Art. 73 deadline."""
        try:
            from novafabric.compliance.incident.store import IncidentStore

            with IncidentStore() as store:
                incidents = store.list_all()
            return {"ok": True, "count": len(incidents),
                    "incidents": [_incident_view(i) for i in incidents]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "incidents": [], "error": str(exc)}

    @app.post("/api/incidents", dependencies=[Depends(verify_token)])
    async def api_incidents_create(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Open a new incident (forward-only lifecycle; never deleted)."""
        from datetime import datetime, timezone

        from novafabric.compliance.incident.models import (
            Incident,
            IncidentSeverity,
        )
        from novafabric.compliance.incident.store import IncidentStore

        title = str(body.get("title", "")).strip()
        classification = str(body.get("classification", "")).strip()
        if not title:
            raise HTTPException(status_code=422, detail="title is required")
        if not classification:
            raise HTTPException(status_code=422, detail="classification is required")
        try:
            severity = IncidentSeverity(str(body.get("severity", "medium")).lower())
            occurred_raw = body.get("occurred_at")
            occurred = (
                datetime.fromisoformat(occurred_raw)
                if occurred_raw
                else datetime.now(timezone.utc)
            )
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=timezone.utc)
            aware_raw = body.get("aware_at")
            aware = None
            if aware_raw:
                aware = datetime.fromisoformat(aware_raw)
                if aware.tzinfo is None:
                    aware = aware.replace(tzinfo=timezone.utc)
            incident = Incident(
                title=title,
                classification=classification,
                severity=severity,
                occurred_at=occurred,
                aware_at=aware,
                run_ids=list(body.get("run_ids", []) or []),
            )
            with IncidentStore() as store:
                created = store.create(incident)
            return {"ok": True, "incident": _incident_view(created)}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.get("/api/incidents/{incident_id}", dependencies=[Depends(verify_token)])
    async def api_incidents_get(incident_id: str) -> dict[str, Any]:
        """Get one incident with computed Art. 73 deadlines."""
        try:
            from novafabric.compliance.incident.store import IncidentStore

            with IncidentStore() as store:
                inc = store.get(incident_id)
            return {"ok": True, "incident": _incident_view(inc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.post("/api/incidents/{incident_id}/transition", dependencies=[Depends(verify_token)])
    async def api_incidents_transition(
        incident_id: str, body: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """Advance an incident's lifecycle (open → reported → closed)."""
        from novafabric.compliance.incident.models import IncidentStatus
        from novafabric.compliance.incident.store import IncidentStore

        to_status = str(body.get("to_status", "")).lower()
        try:
            target = IncidentStatus(to_status)
        except ValueError:
            raise HTTPException(status_code=422, detail=f"invalid status {to_status!r}")
        try:
            with IncidentStore() as store:
                inc = store.get(incident_id)
                updated = store.save(inc.transitioned(target))
            return {"ok": True, "incident": _incident_view(updated)}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    @app.get("/api/incidents/{incident_id}/export", dependencies=[Depends(verify_token)])
    async def api_incidents_export(incident_id: str, fmt: str = "aim") -> dict[str, Any]:
        """Export an incident as an OECD-AIM or NIS2 structured report."""
        try:
            from novafabric.compliance.incident.aim_export import (
                build_aim_report,
                build_nis2_report_from_incident,
            )
            from novafabric.compliance.incident.store import IncidentStore

            with IncidentStore() as store:
                inc = store.get(incident_id)
            if fmt == "nis2":
                report = _dump(build_nis2_report_from_incident(inc))
            else:
                report = build_aim_report(inc)
            return {"ok": True, "format": fmt, "report": report}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ---------- Diagnose — failure attribution (nova diagnose, ADR-0084) ----------

    @app.get("/api/runs/{run_id}/diagnose", dependencies=[Depends(verify_token)])
    async def api_diagnose(run_id: str) -> dict[str, Any]:
        """Attribute a failed run to its most likely responsible step."""
        try:
            from novafabric.diagnose import attribute_failure

            cap = _resolve_capsule(run_id, capsule_dir)
            attribution = attribute_failure(cap)
            return {"ok": True, "run_id": run_id, "attribution": _dump(attribution)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "run_id": run_id, "error": str(exc)}

    # ---------- Evidence assertions (nova evidence, ADR-0087) ----------

    @app.get("/api/evidence/completeness/{run_id}", dependencies=[Depends(verify_token)])
    async def api_evidence_completeness(run_id: str) -> dict[str, Any]:
        """Compute the completeness assertion for a capsule."""
        try:
            from novafabric.evidence.completeness import compute_completeness

            cap = _resolve_capsule(run_id, capsule_dir)
            assertion = compute_completeness(cap, run_id)
            return {"ok": True, "run_id": run_id, "assertion": _dump(assertion)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "run_id": run_id, "error": str(exc)}

    @app.post("/api/evidence/{run_id}/bind", dependencies=[Depends(verify_token)])
    async def api_evidence_bind(
        run_id: str, body: dict[str, Any] = Body(default={})
    ) -> dict[str, Any]:
        """Build criterion-evidence bindings for a capsule against a profile."""
        profile = str(body.get("profile", "nist-ai-rmf"))
        try:
            from novafabric.evidence.binding import build_bindings

            cap = _resolve_capsule(run_id, capsule_dir)
            bindings = build_bindings(cap, profile)
            return {"ok": True, "run_id": run_id, "profile": profile,
                    "count": len(bindings), "bindings": [_dump(b) for b in bindings]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "run_id": run_id, "error": str(exc)}

    # ---------- Seal ratchet (nova seal ratchet, ADR-0089) ----------

    @app.get("/api/seal/ratchet/status", dependencies=[Depends(verify_token)])
    async def api_ratchet_status(node_id: str) -> dict[str, Any]:
        """Show a node's current signing epoch and registry history."""
        try:
            from novafabric.trust.novaseal.ratchet import EpochRegistry, load_state

            state = load_state(node_id)
            records = EpochRegistry().records(node_id)
            return {"ok": True, "state": _dump(state),
                    "registry_epochs": [r.epoch for r in records]}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "node_id": node_id, "error": str(exc)}

    @app.post("/api/seal/ratchet/init", dependencies=[Depends(verify_token)])
    async def api_ratchet_init(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Provision epoch-0 ratchet state for a node."""
        node_id = str(body.get("node_id", "")).strip()
        if not node_id:
            raise HTTPException(status_code=422, detail="node_id is required")
        try:
            from novafabric.trust.novaseal.ratchet import init_ratchet

            state = init_ratchet(node_id)
            return {"ok": True, "state": _dump(state)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "node_id": node_id, "error": str(exc)}

    @app.post("/api/seal/ratchet/rotate", dependencies=[Depends(verify_token)])
    async def api_ratchet_rotate(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Advance to the next signing epoch; erase the previous chain key."""
        node_id = str(body.get("node_id", "")).strip()
        if not body.get("confirmed"):
            raise HTTPException(status_code=400, detail="rotate requires confirmed=true")
        if not node_id:
            raise HTTPException(status_code=422, detail="node_id is required")
        try:
            from novafabric.trust.novaseal.ratchet import rotate

            state = rotate(node_id)
            return {"ok": True, "state": _dump(state)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "node_id": node_id, "error": str(exc)}

    # ---------- Ops — warm capture daemon status (nova daemon status) ----------

    @app.get("/api/ops/daemon-status", dependencies=[Depends(verify_token)])
    async def api_daemon_status() -> dict[str, Any]:
        """Report whether the warm capture daemon socket is alive (read-only)."""
        import socket as _socket

        try:
            from novafabric._paths import daemon_socket_path

            path = daemon_socket_path()
            alive = False
            if path.exists():
                try:
                    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                    sock.connect(str(path))
                    sock.close()
                    alive = True
                except OSError:
                    alive = False
            return {"ok": True, "alive": alive, "socket_path": str(path)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "alive": False, "error": str(exc)}

    # ---------- System card (nova export-system-card, ADR-0085) ----------

    @app.post("/api/runs/{run_id}/export-system-card", dependencies=[Depends(verify_token)])
    async def api_export_system_card(run_id: str) -> dict[str, Any]:
        """Generate and SEAL an auto-generated system/audit card."""
        try:
            from novafabric._paths import nova_home
            from novafabric.compliance.system_card import SystemCardGenerator
            from novafabric.evidence.signing import LocalSigner, generate_keypair

            cap = _resolve_capsule(run_id, capsule_dir)
            keys_dir = nova_home() / "keys"
            priv = keys_dir / "ed25519.pem"
            if not priv.exists():
                generate_keypair(keys_dir)
            signer = LocalSigner(priv)
            generator = SystemCardGenerator()
            card = generator.build_card(cap)
            sealed = generator.seal_card(card, signer)
            return {"ok": True, "run_id": run_id, "card": card, "sealed": sealed}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "run_id": run_id, "error": str(exc)}

    # ---------- Rebuild metadata DB (nova rebuild-metadata-db, FR-04) ----------

    @app.post("/api/admin/rebuild-metadata-db", dependencies=[Depends(verify_token)])
    async def api_rebuild_metadata_db(body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Disaster-recovery rebuild of the metadata DB from the chain log.

        Gated: requires confirmed=true (destructive, global scope).
        """
        if not body.get("confirmed"):
            raise HTTPException(status_code=400, detail="rebuild requires confirmed=true")
        prefix = str(body.get("prefix", ""))
        backend = str(body.get("backend", "local"))
        target_db = str(body.get("target_db", "nova-metadata-rebuild.db"))
        try:
            from novafabric.object_capsule_store.backend_router import make_adapter
            from novafabric.object_capsule_store.rebuild import (
                rebuild_metadata_db as _rebuild,
            )

            adapter = make_adapter(backend=backend)
            report = _rebuild(adapter=adapter, prefix=prefix, target_db=target_db)
            return {
                "ok": True,
                "target_db": target_db,
                "capsules_found": report.capsules_found,
                "time_to_replay_seconds": report.time_to_replay_seconds,
                "integrity_warnings": list(report.integrity_warnings),
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    # ---------- WebSocket catch-all ----------
    # StaticFiles(html=True) mounted at "/" only handles HTTP. Any WebSocket
    # connection that doesn't match an explicit @app.websocket() route above
    # would fall through to StaticFiles and trigger an AssertionError.
    # This catch-all closes unmatched sockets gracefully before that happens.
    @app.websocket("/{ws_path:path}")
    async def _ws_catchall(websocket: WebSocket, ws_path: str) -> None:
        await websocket.close(code=1011)

    # ---------- static dashboard (optional) ----------
    # Mounted LAST so the "/" catch-all does not shadow any explicit routes above.

    if static_dir is not None and static_dir.exists():
        # `html=True` makes Astro's directory-style routes (e.g. /concepts/index.html)
        # resolve when requested as /concepts.
        app.mount(
            "/",
            StaticFiles(directory=str(static_dir), html=True, check_dir=False),
            name="site",
        )
    else:
        @app.get("/")
        async def serve_no_dashboard() -> dict[str, Any]:
            return {
                "service": "nova-serve",
                "experimental": True,
                "dashboard_static": "missing — run `npm run build:dashboard` in web/",
                "api_docs": "/api/docs",
            }

    # ── Reports ──────────────────────────────────────────────────────────────

    def _csv_response(
        columns: list[str], rows: list[dict[str, Any]], filename: str
    ) -> StreamingResponse:
        content = _reports.rows_to_csv(columns, rows)
        return StreamingResponse(
            iter([content]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/reports/run-history", dependencies=[Depends(verify_token)])
    async def report_run_history(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        status: str | None = Query(default=None),
        agent: str | None = Query(default=None),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_run_history(
            capsule_dir, from_ts, to_ts, status, agent, db_path=db_path
        )
        if format == "csv":
            return _csv_response(cols, rows, "run-history.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/cost-burn", dependencies=[Depends(verify_token)])
    async def report_cost_burn(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_cost_burn(capsule_dir, from_ts, to_ts, db_path=db_path)
        if format == "csv":
            return _csv_response(cols, rows, "cost-burn.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/throughput", dependencies=[Depends(verify_token)])
    async def report_throughput(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        resolution: str = Query(default="1d"),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_throughput(capsule_dir, from_ts, to_ts, resolution)
        if format == "csv":
            return _csv_response(cols, rows, "throughput.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/executive-summary", dependencies=[Depends(verify_token)])
    async def report_executive_summary(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_executive_summary(capsule_dir, from_ts, to_ts)
        if format == "csv":
            return _csv_response(cols, rows, "executive-summary.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/evidence-inventory", dependencies=[Depends(verify_token)])
    async def report_evidence_inventory(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_evidence_inventory(from_ts, to_ts)
        if format == "csv":
            return _csv_response(cols, rows, "evidence-inventory.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/eval-regression", dependencies=[Depends(verify_token)])
    async def report_eval_regression(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        suite: str | None = Query(default=None),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_eval_regression(db_path, from_ts, to_ts, suite)
        if format == "csv":
            return _csv_response(cols, rows, "eval-regression.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/policy-audit", dependencies=[Depends(verify_token)])
    async def report_policy_audit(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        policy_id: str | None = Query(default=None),
        result: str | None = Query(default=None),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_policy_audit(db_path, from_ts, to_ts, policy_id, result)
        if format == "csv":
            return _csv_response(cols, rows, "policy-audit.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/seal-verification", dependencies=[Depends(verify_token)])
    async def report_seal_verification(
        from_ts: str | None = Query(default=None, alias="from"),
        to_ts: str | None = Query(default=None, alias="to"),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_seal_verification(db_path, from_ts, to_ts)
        if format == "csv":
            return _csv_response(cols, rows, "seal-verification.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/capsule-compare", dependencies=[Depends(verify_token)])
    async def report_capsule_compare(
        run_a: str = Query(),
        run_b: str = Query(),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_capsule_compare(capsule_dir, run_a, run_b)
        if format == "csv":
            return _csv_response(cols, rows, "capsule-compare.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    @app.get("/api/reports/release-comparison", dependencies=[Depends(verify_token)])
    async def report_release_comparison(
        version_a: str = Query(),
        version_b: str = Query(),
        format: str = Query(default="json"),
    ) -> Any:
        cols, rows = _reports.report_release_comparison(db_path, version_a, version_b)
        if format == "csv":
            return _csv_response(cols, rows, "release-comparison.csv")
        return {"columns": cols, "rows": rows, "count": len(rows)}

    return app


# ---------- helpers ----------


def _parse_prom_gauge(text: str, metric_name: str) -> float | None:
    """Parse a single gauge value from Prometheus text exposition format."""
    for line in text.splitlines():
        if line.startswith(metric_name + " ") or line.startswith(metric_name + "{"):
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                try:
                    return float(parts[1])
                except ValueError:
                    pass
    return None


def _resolve_capsule(run_id: str, capsule_dir: Path) -> Path:
    """Find the capsule directory for a run_id; raise 404 if absent."""
    if "/" in run_id or ".." in run_id:
        raise HTTPException(status_code=400, detail="invalid run_id")
    candidate = capsule_dir / run_id
    if candidate.is_dir() and (candidate / "capsule.yaml").exists():
        return candidate
    # Fallback: scan in case run_id is the manifest's run_id, not the directory name
    for d in discover_capsule_dirs(capsule_dir):
        try:
            m = load_capsule_manifest(d)
        except FileNotFoundError:
            continue
        if m.get("run_id") == run_id:
            return d
    raise HTTPException(status_code=404, detail=f"run not found: {run_id}")


def _strip_spec(asset_row: dict[str, Any]) -> dict[str, Any]:
    """Return a list-friendly subset of the asset row (drops the verbose spec_json)."""
    return {
        "id": asset_row.get("id"),
        "name": asset_row.get("name"),
        "version": asset_row.get("version"),
        "asset_type": asset_row.get("asset_type"),
        "status": asset_row.get("status"),
        "created_at": asset_row.get("created_at"),
        "promoted_at": asset_row.get("promoted_at"),
        "git_commit_sha": asset_row.get("git_commit_sha"),
    }

def _consteq(a: str, b: str) -> bool:
    """Constant-time equality; same as hmac.compare_digest but tolerant of types."""
    if len(a) != len(b):
        return False
    result = 0
    for x, y in zip(a.encode(), b.encode()):
        result |= x ^ y
    return result == 0
