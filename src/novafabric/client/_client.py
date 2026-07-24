"""``NovaFabricClient`` — typed, sync httpx client for the ``/v0`` REST API.

ADR-0202 P1 (experimental). Server-mode tooling: local-first core features
need none of this. The client sends no request other than the ones the caller
invokes — nothing at import or construction time, no telemetry.
"""

from __future__ import annotations

import importlib.metadata
import io
import os
import warnings
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import quote

import httpx

from novafabric.client._errors import (
    NovaFabricConfigError,
    NovaFabricTimeout,
    NovaFabricTransportError,
    error_from_response,
)
from novafabric.client._models import (
    ApiResult,
    AssetDetail,
    AssetSummary,
    CapsuleDetail,
    CapsuleSummary,
    Page,
    ResponseMeta,
    ScoreSubmission,
    ScoreSubmissionResult,
    ServerHealth,
)
from novafabric.client._retry import (
    RetryConfig,
    _sleep,
    compute_backoff,
    parse_retry_after,
)

TokenProvider = Callable[[], str]
"""Zero-arg bearer-token provider — the host application owns refresh."""

_ENV_SERVER_URL = "NOVAFABRIC_SERVER_URL"
_ENV_API_KEY = "NOVAFABRIC_API_KEY"
_ENV_TOKEN = "NOVAFABRIC_TOKEN"

_QUOTA_WARNING_HEADER = "X-NovaFabric-Quota-Warning"

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)

# Warn-once-per-endpoint registry for Deprecation/Sunset headers (ADR-0188),
# mirroring the TS SDK's process-wide `warnedEndpoints` set.
_warned_endpoints: set[str] = set()


def reset_deprecation_warnings() -> None:
    """Test helper: clear the process-wide deprecation warn-once registry."""
    _warned_endpoints.clear()


def _client_version() -> str:
    try:
        return importlib.metadata.version("novafabric")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _meta_from_response(response: httpx.Response) -> ResponseMeta:
    return ResponseMeta(
        status=response.status_code,
        deprecation=response.headers.get("Deprecation"),
        sunset=response.headers.get("Sunset"),
        quota_warning=response.headers.get(_QUOTA_WARNING_HEADER),
        request_id=response.headers.get("X-Request-ID"),
    )


def _surface_deprecation(endpoint: str, meta: ResponseMeta) -> None:
    if meta.deprecation is None and meta.sunset is None:
        return
    if endpoint in _warned_endpoints:
        return
    _warned_endpoints.add(endpoint)
    parts = [f"NovaFabric: endpoint {endpoint} is deprecated"]
    if meta.deprecation is not None:
        parts.append(f"Deprecation: {meta.deprecation}")
    if meta.sunset is not None:
        parts.append(f"Sunset: {meta.sunset}")
    parts.append("See the API deprecation register (ADR-0188).")
    warnings.warn(" — ".join(parts), DeprecationWarning, stacklevel=4)


def _zip_directory(directory: Path) -> bytes:
    """Pack *directory* into a deterministic in-memory ZIP.

    Entries are sorted and rooted at the directory name (the server strips one
    leading path component on extract); timestamps are pinned so identical
    trees produce identical bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        files = sorted(p for p in directory.rglob("*") if p.is_file())
        for path in files:
            arcname = f"{directory.name}/{path.relative_to(directory).as_posix()}"
            info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return buffer.getvalue()


class NovaFabricClient:
    """Typed, sync client for the NovaFabric ``/v0`` REST API (experimental).

    There is **no default server URL**: pass ``base_url`` (including the
    ``/v0`` prefix, e.g. ``"https://nova.example.com/v0"``) or set
    ``NOVAFABRIC_SERVER_URL``. Credentials travel as a single
    ``Authorization: Bearer`` header: pass ``api_key`` (an ``nvfk_`` key,
    ADR-0193) **or** ``token`` (OIDC access / offline / local token — a string
    or a zero-arg callable), never both.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        token: str | TokenProvider | None = None,
        timeout: httpx.Timeout | float | None = None,
        retries: RetryConfig | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        resolved_url = base_url if base_url is not None else os.environ.get(_ENV_SERVER_URL)
        if not resolved_url:
            raise NovaFabricConfigError(
                "NovaFabricClient requires a base_url (there is no default server "
                "URL). Pass base_url or set NOVAFABRIC_SERVER_URL. Example: "
                'NovaFabricClient("https://nova.example.com/v0")'
            )
        if api_key is not None and token is not None:
            raise NovaFabricConfigError(
                "api_key and token are mutually exclusive — pass exactly one."
            )
        if api_key is None and token is None:
            # Env credential lookup only when neither argument was given.
            env_api_key = os.environ.get(_ENV_API_KEY)
            env_token = os.environ.get(_ENV_TOKEN)
            if env_api_key and env_token:
                warnings.warn(
                    "Both NOVAFABRIC_API_KEY and NOVAFABRIC_TOKEN are set; "
                    "NOVAFABRIC_API_KEY wins and NOVAFABRIC_TOKEN is ignored.",
                    UserWarning,
                    stacklevel=2,
                )
                env_token = None
            api_key = env_api_key or None
            if api_key is None:
                token = env_token or None

        self._base_url = resolved_url.rstrip("/")
        self._api_key = api_key
        self._token = token
        self._retries = retries if retries is not None else RetryConfig()

        if timeout is None:
            resolved_timeout: httpx.Timeout = _DEFAULT_TIMEOUT
        elif isinstance(timeout, httpx.Timeout):
            resolved_timeout = timeout
        else:
            resolved_timeout = httpx.Timeout(timeout)

        # Constructing the pool performs no I/O; no request leaves before a
        # method is invoked (spec acceptance criterion 1).
        self._http = httpx.Client(timeout=resolved_timeout, transport=transport)

    # ------------------------------ server --------------------------------

    def health(self) -> ApiResult[ServerHealth]:
        """``GET <root>/health`` — unauthenticated liveness + server version."""
        root = self._base_url.removesuffix("/v0")
        response, meta = self._send(
            "GET", "/health", "GET /health", absolute_url=f"{root}/health"
        )
        return ApiResult(ServerHealth.model_validate(response.json()), meta)

    # ----------------------------- capsules -------------------------------

    def get_capsule(self, run_id: str) -> ApiResult[CapsuleDetail]:
        """``GET /capsules/{run_id}`` — capsule detail by run ID."""
        response, meta = self._send(
            "GET",
            f"/capsules/{quote(run_id, safe='')}",
            "GET /capsules/{run_id}",
        )
        return ApiResult(CapsuleDetail.model_validate(response.json()), meta)

    def list_capsules(
        self, *, limit: int | None = None, cursor: str | None = None
    ) -> ApiResult[Page[CapsuleSummary]]:
        """``GET /capsules`` — one page of capsule summaries."""
        response, meta = self._send(
            "GET",
            "/capsules",
            "GET /capsules",
            params={"limit": limit, "cursor": cursor},
        )
        return ApiResult(Page[CapsuleSummary].model_validate(response.json()), meta)

    def iter_capsules(self, *, limit: int | None = None) -> Iterator[CapsuleSummary]:
        """Lazily iterate ALL capsules, walking ``next_cursor`` pages.

        Fetches page *n+1* only when iteration exhausts page *n*; never
        buffers more than one page; treats cursors as opaque.
        """
        cursor: str | None = None
        while True:
            page = self.list_capsules(limit=limit, cursor=cursor).data
            yield from page.items
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    def upload_capsule(self, source: Path | bytes) -> ApiResult[CapsuleSummary]:
        """``POST /capsules`` — upload a capsule ZIP (multipart, never retried).

        *source* is a directory (packed to a deterministic in-memory ZIP), a
        path to an existing ``.zip`` file, or raw ZIP ``bytes``.
        """
        if isinstance(source, bytes):
            payload = source
        elif source.is_dir():
            payload = _zip_directory(source)
        else:
            payload = source.read_bytes()
        response, meta = self._send(
            "POST",
            "/capsules",
            "POST /capsules",
            files={"capsule": ("capsule.zip", payload, "application/zip")},
        )
        return ApiResult(CapsuleSummary.model_validate(response.json()), meta)

    # ------------------------------ assets --------------------------------

    def get_asset(self, asset_id: str) -> ApiResult[AssetDetail]:
        """``GET /assets/{id}`` — asset detail by registry UUID."""
        response, meta = self._send(
            "GET",
            f"/assets/{quote(asset_id, safe='')}",
            "GET /assets/{id}",
        )
        return ApiResult(AssetDetail.model_validate(response.json()), meta)

    def list_assets(
        self,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        asset_type: str | None = None,
        status: str | None = None,
    ) -> ApiResult[Page[AssetSummary]]:
        """``GET /assets`` — one page of asset summaries."""
        response, meta = self._send(
            "GET",
            "/assets",
            "GET /assets",
            params={
                "limit": limit,
                "cursor": cursor,
                "asset_type": asset_type,
                "status": status,
            },
        )
        return ApiResult(Page[AssetSummary].model_validate(response.json()), meta)

    def iter_assets(
        self,
        *,
        limit: int | None = None,
        asset_type: str | None = None,
        status: str | None = None,
    ) -> Iterator[AssetSummary]:
        """Lazily iterate ALL assets matching the filters (see iter_capsules)."""
        cursor: str | None = None
        while True:
            page = self.list_assets(
                limit=limit, cursor=cursor, asset_type=asset_type, status=status
            ).data
            yield from page.items
            if not page.next_cursor:
                return
            cursor = page.next_cursor

    # ------------------------------ scores --------------------------------

    def submit_score(
        self,
        run_id: str,
        score: ScoreSubmission | dict[str, Any],
    ) -> ApiResult[ScoreSubmissionResult | None]:
        """``POST /capsules/{run_id}/scores`` — submit an external score.

        ``201`` ⇒ ``data`` is the stored record; a ``200`` idempotent replay
        carries no body ⇒ ``data is None`` (inspect ``meta.status``).
        Never auto-retried.
        """
        body = (
            score.model_dump(exclude_none=True)
            if isinstance(score, ScoreSubmission)
            else score
        )
        response, meta = self._send(
            "POST",
            f"/capsules/{quote(run_id, safe='')}/scores",
            "POST /capsules/{run_id}/scores",
            json_body=body,
        )
        if meta.status == 200:
            return ApiResult(None, meta)
        return ApiResult(ScoreSubmissionResult.model_validate(response.json()), meta)

    # ----------------------------- lifecycle ------------------------------

    def close(self) -> None:
        """Close the underlying connection pool."""
        self._http.close()

    def __enter__(self) -> NovaFabricClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ----------------------------- internals ------------------------------

    def _auth_header(self) -> str | None:
        if self._api_key is not None:
            return f"Bearer {self._api_key}"
        if self._token is None:
            return None
        raw = self._token() if callable(self._token) else self._token
        return f"Bearer {raw}"

    def _send(
        self,
        method: str,
        path: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        absolute_url: str | None = None,
    ) -> tuple[httpx.Response, ResponseMeta]:
        """Shared request core: auth, bounded GET-only retries, deprecation
        surfacing, typed errors on any non-2xx."""
        url = absolute_url if absolute_url is not None else f"{self._base_url}{path}"
        query = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {
            "Accept": "application/json",
            "User-Agent": f"novafabric-python/{_client_version()}",
        }
        auth = self._auth_header()
        if auth is not None:
            headers["Authorization"] = auth

        config = self._retries
        max_attempts = config.max_attempts if method == "GET" else 1
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._http.request(
                    method,
                    url,
                    params=query or None,
                    json=json_body,
                    files=files,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                # Only connect-phase failures are retryable (the request never
                # reached the server, so a GET retry is always safe).
                connect_phase = isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))
                if connect_phase and attempt < max_attempts:
                    _sleep(compute_backoff(config, attempt - 1))
                    continue
                message = str(exc) or type(exc).__name__
                if isinstance(exc, httpx.TimeoutException):
                    raise NovaFabricTimeout(message, cause=exc) from exc
                raise NovaFabricTransportError(message, cause=exc) from exc

            if response.status_code in config.retry_statuses and attempt < max_attempts:
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                delay = (
                    retry_after
                    if retry_after is not None
                    else compute_backoff(config, attempt - 1)
                )
                _sleep(delay)
                continue

            meta = _meta_from_response(response)
            _surface_deprecation(endpoint, meta)
            if response.status_code >= 400:
                raise error_from_response(response, meta)
            return response, meta
