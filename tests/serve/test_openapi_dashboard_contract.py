"""Dashboard OpenAPI contract ratchet + conformance (ADR-0240 slice 1).

Three mechanisms, copied from the proven ADR-0227 pattern:

1. **Ratchet** — every HTTP route on the canonical serve app is either
   annotated (an explicit ``dashboard*`` ``operation_id`` + a declared success
   response) or listed, exactly, in ``openapi_dashboard_pending.txt``. A new
   route fails here by name until it is annotated or (discouraged) listed; an
   annotated route still on the list fails too, so the list can only shrink.
2. **Declaration invariants** — annotated routes must not bind a
   ``response_model`` (it filters the wire body; the ADR-0227 lesson), and
   their operation ids are unique.
3. **Drift** — ``api/openapi-dashboard.yaml`` must match regeneration
   byte-for-byte (generated, never hand-edited — the BL-041 lesson).

Scope: the canonical ``create_app`` configuration. Conditionally mounted
surfaces (TV-5 topology, static) join the ratchet when they join the app.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

pytest.importorskip("fastapi")

from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Iterator

REPO = Path(__file__).resolve().parents[2]
PENDING_FILE = Path(__file__).with_name("openapi_dashboard_pending.txt")

VALID_TOKEN = "test-token-1234567890abcdef"
HEADERS = {"host": "127.0.0.1:4321", "Authorization": f"Bearer {VALID_TOKEN}"}


def _load_generator() -> Any:
    spec = importlib.util.spec_from_file_location(
        "gen_openapi_dashboard", REPO / "scripts" / "gen_openapi_dashboard.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def contract_app() -> Any:
    return _load_generator().build_app()


def _api_routes(app: Any) -> list[tuple[str, APIRoute]]:
    out = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((f"{method} {route.path}", route))
    return out


def _is_annotated(route: APIRoute) -> bool:
    return (route.operation_id or "").startswith("dashboard")


def test_every_route_is_annotated_or_exactly_pending(contract_app: Any) -> None:
    listed = {ln for ln in PENDING_FILE.read_text().splitlines() if ln.strip()}

    actual_pending = {
        ident for ident, route in _api_routes(contract_app) if not _is_annotated(route)
    }

    new_unlisted = actual_pending - listed
    assert not new_unlisted, (
        "routes neither annotated (operation_id + responses=) nor in "
        f"openapi_dashboard_pending.txt — annotate them (preferred): {sorted(new_unlisted)}"
    )
    stale_listed = listed - actual_pending
    assert not stale_listed, (
        "openapi_dashboard_pending.txt lists routes that are now annotated or gone — "
        f"remove them so the ratchet only shrinks: {sorted(stale_listed)}"
    )


def test_annotated_routes_declare_and_do_not_bind(contract_app: Any) -> None:
    seen_ids: dict[str, str] = {}
    annotated = 0
    for ident, route in _api_routes(contract_app):
        if not _is_annotated(route):
            continue
        annotated += 1
        op_id = route.operation_id or ""
        assert route.responses, f"{ident}: annotated but declares no responses="
        assert route.response_model is None, (
            f"{ident}: binds response_model — declared models must not filter "
            "the wire body (ADR-0227 invariant)"
        )
        assert op_id not in seen_ids, (
            f"duplicate operation_id {op_id!r}: {ident} and {seen_ids[op_id]}"
        )
        seen_ids[op_id] = ident
    assert annotated >= 3, "the holds family should be annotated (slice 1)"


def test_committed_contract_matches_regeneration() -> None:
    committed = REPO / "api" / "openapi-dashboard.yaml"
    assert committed.is_file(), (
        "api/openapi-dashboard.yaml missing — run scripts/gen_openapi_dashboard.py"
    )
    assert committed.read_text() == _load_generator().render(), (
        "api/openapi-dashboard.yaml is stale — run: "
        "uv run python scripts/gen_openapi_dashboard.py"
    )


class TestHoldsConformance:
    """Real responses of the first annotated family validate against the
    models their routes declare — read off the live route, ADR-0227 style."""

    @pytest.fixture()
    def client(self, tmp_path: Path) -> "Iterator[TestClient]":
        from novafabric.serve.app import create_app

        base = tmp_path / "runs"
        base.mkdir()
        app = create_app(
            token=VALID_TOKEN,
            capsule_dir=base,
            db_path=tmp_path / "r.db",
            static_dir=None,
        )
        with TestClient(app) as c:
            yield c

    @staticmethod
    def _declared_model(app: Any, op_id: str) -> Any:
        for route in app.routes:
            if isinstance(route, APIRoute) and route.operation_id == op_id:
                decl = route.responses.get(200)
                assert isinstance(decl, dict) and "model" in decl, (
                    f"{op_id}: no declared 200 model"
                )
                return decl["model"]
        raise AssertionError(f"no route with operation_id {op_id}")

    def test_place_list_release_round_trip(self, client: TestClient) -> None:
        app = client.app
        created = client.post(
            "/api/holds",
            headers=HEADERS,
            json={"registry": "reg-a", "reason": "litigation"},
        )
        assert created.status_code == 200, created.text
        self._declared_model(app, "dashboardPlaceHold").model_validate(created.json())
        hold_id = created.json()["hold_id"]

        listed = client.get("/api/holds", headers=HEADERS)
        assert listed.status_code == 200
        body = self._declared_model(app, "dashboardListHolds").model_validate(
            listed.json()
        )
        assert body.total_active == 1

        released = client.post(f"/api/holds/{hold_id}/release", headers=HEADERS)
        assert released.status_code == 200
        rel = self._declared_model(app, "dashboardReleaseHold").model_validate(
            released.json()
        )
        assert rel.released is True and rel.hold_id == hold_id
