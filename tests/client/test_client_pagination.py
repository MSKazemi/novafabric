"""Cursor pagination + lazy iterators (ADR-0202 D6; spec §Pagination).

Iterators are lazy (≤1 page buffered), terminate on ``next_cursor: null``,
and treat cursors as opaque strings.
"""

from __future__ import annotations

from typing import Any

import httpx

from novafabric.client import NovaFabricClient, Page

from .conftest import ScriptedTransport


def _page(items: list[dict[str, Any]], next_cursor: str | None, total: int) -> httpx.Response:
    return httpx.Response(
        200, json={"items": items, "next_cursor": next_cursor, "total": total}
    )


def _capsule(run_id: str) -> dict[str, Any]:
    return {"run_id": run_id, "status": "success"}


class TestListPage:
    def test_list_returns_one_page_verbatim(self) -> None:
        transport = ScriptedTransport(_page([_capsule("a")], "CURSOR-1", 3))
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        result = client.list_capsules(limit=1)
        page = result.data
        assert isinstance(page, Page)
        assert [c.run_id for c in page.items] == ["a"]
        assert page.next_cursor == "CURSOR-1"
        assert page.total == 3
        assert len(transport.requests) == 1

    def test_cursor_passed_through_verbatim(self) -> None:
        transport = ScriptedTransport(_page([], None, 0))
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        client.list_capsules(cursor="OPAQUE==weird//token")
        assert (
            transport.requests[0].url.params["cursor"] == "OPAQUE==weird//token"
        )

    def test_extra_server_fields_never_raise(self) -> None:
        body = {
            "items": [{"run_id": "a", "brand_new_field": {"x": 1}}],
            "next_cursor": None,
            "total": 1,
            "server_added_key": True,
        }
        transport = ScriptedTransport(httpx.Response(200, json=body))
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        page = client.list_capsules().data
        assert page.items[0].run_id == "a"


class TestIterCapsules:
    def test_walks_all_pages_and_terminates_on_null_cursor(self) -> None:
        transport = ScriptedTransport(
            _page([_capsule("a"), _capsule("b")], "c1", 5),
            _page([_capsule("c"), _capsule("d")], "c2", 5),
            _page([_capsule("e")], None, 5),
        )
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        run_ids = [c.run_id for c in client.iter_capsules(limit=2)]
        assert run_ids == ["a", "b", "c", "d", "e"]
        assert len(transport.requests) == 3
        # The opaque cursor from page n is passed to page n+1.
        assert transport.requests[1].url.params["cursor"] == "c1"
        assert transport.requests[2].url.params["cursor"] == "c2"

    def test_lazy_one_request_per_page(self) -> None:
        transport = ScriptedTransport(
            _page([_capsule("a")], "c1", 2),
            _page([_capsule("b")], None, 2),
        )
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        iterator = client.iter_capsules()
        assert transport.requests == []  # nothing fetched before first next()
        first = next(iterator)
        assert first.run_id == "a"
        assert len(transport.requests) == 1  # page 2 not fetched yet
        second = next(iterator)
        assert second.run_id == "b"
        assert len(transport.requests) == 2

    def test_empty_first_page_terminates_after_one_request(self) -> None:
        transport = ScriptedTransport(_page([], None, 0))
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        assert list(client.iter_capsules()) == []
        assert len(transport.requests) == 1

    def test_missing_next_cursor_key_terminates(self) -> None:
        transport = ScriptedTransport(
            httpx.Response(200, json={"items": [_capsule("a")], "total": 1})
        )
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        assert [c.run_id for c in client.iter_capsules()] == ["a"]
        assert len(transport.requests) == 1


class TestIterAssets:
    def test_filters_forwarded_and_pages_walked(self) -> None:
        transport = ScriptedTransport(
            _page([{"id": "1", "name": "m"}], "n1", 2),
            _page([{"id": "2", "name": "m2"}], None, 2),
        )
        client = NovaFabricClient("http://x.example/v0", transport=transport)
        ids = [a.id for a in client.iter_assets(asset_type="model", status="prod")]
        assert ids == ["1", "2"]
        params = transport.requests[0].url.params
        assert params["asset_type"] == "model"
        assert params["status"] == "prod"
        assert transport.requests[1].url.params["cursor"] == "n1"
