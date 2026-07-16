"""Tests for the API deprecation & sunset mechanism (ADR-0188).

Contract (both ways):
  - a route wrapped with ``deprecated(...)`` emits ``Deprecation:``,
    ``Sunset:`` (RFC 8594 HTTP-date) and ``Link: …; rel="deprecation"``;
  - an unwrapped route emits none of the three;
  - the register records the route; production code keeps it EMPTY
    (nothing is deprecated today — import-time check).
"""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.deprecation import (  # noqa: E402
    DEPRECATION_REGISTER,
    DeprecationConfigError,
    deprecated,
    deprecation_register,
)

SUNSET = "2027-01-15"
LINK = "https://example.com/docs/api-reference#deprecation-register"


@pytest.fixture(autouse=True)
def _clean_register():
    """Each test starts with an empty register and restores prior state."""
    saved = list(DEPRECATION_REGISTER)
    DEPRECATION_REGISTER.clear()
    yield
    DEPRECATION_REGISTER[:] = saved


def _make_app(**kwargs: str) -> FastAPI:
    app = FastAPI()
    dep = deprecated(SUNSET, LINK, successor="/v0/new", since="0.60.0", **kwargs)

    @app.get("/v0/old", dependencies=[dep])
    async def old() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v0/fresh")
    async def fresh() -> dict[str, bool]:
        return {"ok": True}

    return app


# --------------------------------------------------------------------------- #
# Headers on a deprecated route
# --------------------------------------------------------------------------- #


def test_deprecated_route_emits_all_three_headers() -> None:
    client = TestClient(_make_app())
    resp = client.get("/v0/old")
    assert resp.status_code == 200
    assert resp.headers["Deprecation"] == "true"
    assert resp.headers["Sunset"].endswith(" GMT")
    assert resp.headers["Link"] == f'<{LINK}>; rel="deprecation"'


def test_sunset_header_is_valid_http_date() -> None:
    client = TestClient(_make_app())
    sunset = client.get("/v0/old").headers["Sunset"]
    parsed = parsedate_to_datetime(sunset)  # raises if not an HTTP-date
    assert parsed == datetime(2027, 1, 15, tzinfo=timezone.utc)
    # IMF-fixdate round-trip (RFC 8594 requires HTTP-date)
    from email.utils import format_datetime

    assert format_datetime(parsed, usegmt=True) == sunset


def test_deprecation_header_unix_timestamp_form() -> None:
    client = TestClient(_make_app(deprecated_at="2026-07-16"))
    value = client.get("/v0/old").headers["Deprecation"]
    expected = int(datetime(2026, 7, 16, tzinfo=timezone.utc).timestamp())
    assert value == f"@{expected}"


def test_link_header_rel_is_deprecation() -> None:
    client = TestClient(_make_app())
    link = client.get("/v0/old").headers["Link"]
    assert link.startswith(f"<{LINK}>")
    assert 'rel="deprecation"' in link


# --------------------------------------------------------------------------- #
# Unwrapped routes stay clean
# --------------------------------------------------------------------------- #


def test_unwrapped_route_emits_none_of_the_three_headers() -> None:
    client = TestClient(_make_app())
    resp = client.get("/v0/fresh")
    assert resp.status_code == 200
    for header in ("Deprecation", "Sunset", "Link"):
        assert header not in resp.headers


# --------------------------------------------------------------------------- #
# Register
# --------------------------------------------------------------------------- #


def test_register_records_declaration_and_binds_route_on_first_request() -> None:
    client = TestClient(_make_app())
    register = deprecation_register()
    assert len(register) == 1
    entry = register[0]
    assert entry.link == LINK
    assert entry.successor == "/v0/new"
    assert entry.since == "0.60.0"
    assert entry.sunset == "Fri, 15 Jan 2027 00:00:00 GMT"
    assert entry.route is None  # not bound until first request
    client.get("/v0/old")
    assert deprecation_register()[0].route == "/v0/old"


def test_register_snapshot_is_immutable_tuple() -> None:
    assert deprecation_register() == ()
    _make_app()
    assert isinstance(deprecation_register(), tuple)
    assert len(deprecation_register()) == 1


def test_register_starts_empty_in_production_code() -> None:
    """Importing the whole server app must not populate the register.

    Runs in a subprocess so no test-local ``deprecated(...)`` calls leak in:
    nothing is deprecated today (ADR-0188 ships the mechanism only).
    """
    code = (
        "import novafabric.server.app, novafabric.server.deprecation as d; "
        "assert d.deprecation_register() == (), d.deprecation_register()"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# --------------------------------------------------------------------------- #
# Configuration errors fail fast at declaration time
# --------------------------------------------------------------------------- #


def test_invalid_sunset_raises_config_error() -> None:
    with pytest.raises(DeprecationConfigError):
        deprecated("not-a-date", LINK)
    assert deprecation_register() == ()  # nothing half-registered


def test_invalid_deprecated_at_raises_config_error() -> None:
    with pytest.raises(DeprecationConfigError):
        deprecated(SUNSET, LINK, deprecated_at="soonish")
    assert deprecation_register() == ()


def test_sunset_accepts_full_iso_datetime() -> None:
    deprecated("2027-01-15T23:59:59+00:00", LINK)
    assert deprecation_register()[0].sunset == "Fri, 15 Jan 2027 23:59:59 GMT"
