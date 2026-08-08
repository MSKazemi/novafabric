"""OpenAPI schema generation must survive the conditionally-mounted TV-5 routes.

The dashboard contract ratchet in ``test_openapi_dashboard_contract`` is scoped
to the canonical ``create_app`` configuration and says so in its own docstring:
"Conditionally mounted surfaces (TV-5 topology, static) join the ratchet when
they join the app." That left a hole exactly the size of this bug — every
production ``nova serve`` on a topology-enabled host answers ``/api/openapi.json``
with a 500 while every test stays green, because no test ever generated the
schema with ``topology_enabled=True``.

Root cause: ``/topology/clusters`` and ``/metrics/stream`` are annotated
``-> _StarletteResponse`` where ``_StarletteResponse`` is imported *inside* the
enclosing factory function. FastAPI treats a return annotation as a response
model, and Pydantic cannot resolve that function-local forward reference at
schema-generation time, raising ``PydanticUserError: ... is not fully defined``.
The fix is ``response_model=None`` on both decorators (the same remedy already
applied at ``router_tv5.py:75``).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.serve.app import create_app  # noqa: E402

VALID_TOKEN = "test-token-openapi-topo-123"
LOCALHOST_HEADERS = {"host": "127.0.0.1:4321"}


def _topology_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NOVA_DASHBOARD_DUCKDB_PATH", str(tmp_path / "topology.duckdb"))
    capsule_dir = tmp_path / "runs"
    capsule_dir.mkdir()
    return create_app(
        token=VALID_TOKEN,
        capsule_dir=capsule_dir,
        db_path=tmp_path / "registry.db",
        static_dir=None,
        topology_enabled=True,
    )


def test_openapi_schema_generates_with_topology_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``app.openapi()`` must not raise when the TV-5 routes are mounted."""
    app = _topology_app(tmp_path, monkeypatch)

    schema = app.openapi()

    assert schema["openapi"].startswith("3.")
    # The two routes that carry the function-local return annotation.
    assert "/topology/clusters" in schema["paths"]
    assert "/metrics/stream" in schema["paths"]


def test_openapi_json_endpoint_200_with_topology_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The served ``/api/openapi.json`` must be 200, not a 500 traceback.

    This is the exact request the dashboard's ``/api/docs`` page makes.
    """
    app = _topology_app(tmp_path, monkeypatch)

    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get(
            "/api/openapi.json",
            headers={**LOCALHOST_HEADERS, "Authorization": f"Bearer {VALID_TOKEN}"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["openapi"].startswith("3.")


def test_topology_routes_declare_no_response_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: a raw-Response route must never bind a response model.

    Re-adding a bare ``-> _StarletteResponse`` annotation without
    ``response_model=None`` is what broke schema generation; assert the
    resolved routes carry no response field so the failure cannot silently
    return.
    """
    from novafabric.serve.introspect import iter_routes

    app = _topology_app(tmp_path, monkeypatch)

    checked = 0
    for _info in iter_routes(app):
        route = _info.route
        if _info.path in {
            "/topology/clusters",
            "/metrics/stream",
        }:
            checked += 1
            assert route.response_model is None, (
                f"{route.path} binds a response_model; raw Response routes must "
                "pass response_model=None"
            )
    assert checked == 2, f"expected both TV-5 routes to be mounted, saw {checked}"
