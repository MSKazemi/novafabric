"""Guards for the HTTP route introspection contract.

The companion to ``tests/cli/test_introspect.py``, and the same failure mode:
walking a framework's internal structure with ``isinstance`` breaks when the
framework changes that structure, and the break reads like "the app lost its
endpoints".

FastAPI 0.141 stopped flattening included routers into ``app.routes``, replacing
each with a lazy ``_IncludedRouter``. Every ``isinstance(r, APIRoute)`` filter
then returned nothing while the endpoints kept serving traffic normally.

These tests are written against behaviour, not against a FastAPI version, so
they pass on the pinned version and on the one the project has not adopted yet.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import APIRouter, FastAPI

from novafabric.serve.introspect import iter_routes, route_methods, route_paths


@pytest.fixture
def app() -> FastAPI:
    """An app whose routes arrive by every mounting route available."""
    application = FastAPI()

    @application.get("/direct")
    def direct() -> dict[str, Any]:  # pragma: no cover - never called
        return {}

    included = APIRouter()

    @included.get("/v0/assets/{asset_id}")
    def get_asset(asset_id: str) -> dict[str, Any]:  # pragma: no cover
        return {}

    @included.post("/v0/assets/{asset_id}")
    def post_asset(asset_id: str) -> dict[str, Any]:  # pragma: no cover
        return {}

    application.include_router(included)

    nested_inner = APIRouter()

    @nested_inner.delete("/deep")
    def deep() -> dict[str, Any]:  # pragma: no cover
        return {}

    nested_outer = APIRouter()
    nested_outer.include_router(nested_inner, prefix="/nested")
    application.include_router(nested_outer)

    return application


def test_finds_directly_registered_routes(app: FastAPI) -> None:
    assert "/direct" in route_paths(app)


def test_finds_routes_from_an_included_router(app: FastAPI) -> None:
    """The exact case FastAPI 0.141 broke."""
    assert "/v0/assets/{asset_id}" in route_paths(app)


def test_finds_routes_through_nested_includes(app: FastAPI) -> None:
    assert "/nested/deep" in route_paths(app)


def test_reports_each_method_separately(app: FastAPI) -> None:
    methods = route_methods(app)
    assert ("/v0/assets/{asset_id}", "GET") in methods
    assert ("/v0/assets/{asset_id}", "POST") in methods
    assert ("/nested/deep", "DELETE") in methods


def test_paths_are_templates_not_raw(app: FastAPI) -> None:
    """Cardinality and privacy both depend on this staying a template."""
    assert all("{asset_id}" in p or "asset" not in p for p in route_paths(app))


def test_exposes_the_underlying_route_object(app: FastAPI) -> None:
    """Callers need more than the path — operation_id, response_model, responses."""
    for info in iter_routes(app):
        if info.path == "/v0/assets/{asset_id}":
            assert hasattr(info.route, "operation_id")
            return
    pytest.fail("route not found")


def test_works_on_a_bare_router_not_only_an_app() -> None:
    router = APIRouter()

    @router.get("/solo")
    def solo() -> dict[str, Any]:  # pragma: no cover
        return {}

    assert "/solo" in route_paths(router)


def test_skips_entries_that_are_not_http_routes(app: FastAPI) -> None:
    """No entry may be yielded without both a path and methods."""
    for info in iter_routes(app):
        assert isinstance(info.path, str) and info.path
        assert info.methods


def test_a_self_referential_mount_raises_rather_than_hanging() -> None:
    """A cycle is a config error; it must fail fast, not spin."""

    class Cyclic:
        def __init__(self) -> None:
            self.routes = [self]

    with pytest.raises(RecursionError, match="points at its own parent"):
        route_paths(Cyclic())


def test_empty_app_yields_no_api_routes() -> None:
    assert route_paths(FastAPI()) <= {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
