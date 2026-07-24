"""Response-meta surfacing: Deprecation/Sunset warn-once, quota warning,
request id (ADR-0202 D8; spec §Version-skew and header behavior)."""

from __future__ import annotations

import warnings

import httpx

from novafabric.client import (
    NovaFabricClient,
    reset_deprecation_warnings,
)

from .conftest import ScriptedTransport


def _ok(headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={"items": [], "next_cursor": None, "total": 0},
        headers=headers or {},
    )


def _deprecated() -> httpx.Response:
    return _ok(
        {
            "Deprecation": "@1735689600",
            "Sunset": "Sat, 01 Aug 2026 00:00:00 GMT",
        }
    )


class TestDeprecationSurfacing:
    def test_headers_copied_into_meta(self) -> None:
        transport = ScriptedTransport(_deprecated())
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            meta = client.list_capsules().meta
        assert meta.deprecation == "@1735689600"
        assert meta.sunset == "Sat, 01 Aug 2026 00:00:00 GMT"

    def test_warns_once_per_endpoint(self) -> None:
        transport = ScriptedTransport(_deprecated())
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client.list_capsules()
            client.list_capsules()
        deprecations = [w for w in caught if w.category is DeprecationWarning]
        assert len(deprecations) == 1
        text = str(deprecations[0].message)
        assert "GET /capsules" in text
        assert "@1735689600" in text
        assert "Sat, 01 Aug 2026 00:00:00 GMT" in text

    def test_distinct_endpoints_warn_separately(self) -> None:
        transport = ScriptedTransport(_deprecated())
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client.list_capsules()
            client.list_assets()
        deprecations = [w for w in caught if w.category is DeprecationWarning]
        assert len(deprecations) == 2

    def test_reset_helper_clears_registry(self) -> None:
        transport = ScriptedTransport(_deprecated())
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client.list_capsules()
            reset_deprecation_warnings()
            client.list_capsules()
        deprecations = [w for w in caught if w.category is DeprecationWarning]
        assert len(deprecations) == 2

    def test_no_headers_no_warning(self) -> None:
        transport = ScriptedTransport(_ok())
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            meta = client.list_capsules().meta
        assert [w for w in caught if w.category is DeprecationWarning] == []
        assert meta.deprecation is None
        assert meta.sunset is None


class TestQuotaAndRequestId:
    def test_quota_warning_copied_verbatim_without_warning(self) -> None:
        transport = ScriptedTransport(
            _ok({"X-NovaFabric-Quota-Warning": "storage 9GiB/10GiB"})
        )
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            meta = client.list_capsules().meta
        assert meta.quota_warning == "storage 9GiB/10GiB"
        assert caught == []  # operator signal, not a deprecation

    def test_request_id_surfaced_when_present(self) -> None:
        transport = ScriptedTransport(_ok({"X-Request-ID": "req-42"}))
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        assert client.list_capsules().meta.request_id == "req-42"
