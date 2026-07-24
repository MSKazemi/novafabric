"""NF-039 — SEP-1649 MCP Server Card (spec NF-038-039-mcp-2026-conformance.md).

Covers R7 (nova serve publishes the card), R8 (validation), AC3 (a real
GET returns a card that validates), and the spec's §5 rejection of a
hand-written card: it is generated from live config so it cannot drift.
"""

from __future__ import annotations

import json

import pytest

from novafabric.mcp.servercard import (
    MCP_PROTOCOL_VERSION,
    REQUIRED_FIELDS,
    WELL_KNOWN_PATH,
    ServerCardValidationError,
    build_server_card,
    validate_server_card,
)


class _Config:
    """Duck-typed ServerConfig stand-in."""

    def __init__(self, **kw: object) -> None:
        for key, value in kw.items():
            setattr(self, key, value)


# ---------------------------------------------------------------------------
# Generation — from live config, never hand-written (spec §5)
# ---------------------------------------------------------------------------


def test_card_advertises_the_2026_07_28_protocol() -> None:
    card = build_server_card()
    assert card.protocolVersion == MCP_PROTOCOL_VERSION == "2026-07-28"


def test_tasks_is_advertised_as_an_extension_not_a_core_capability() -> None:
    """2026-07-28 moved Tasks out of core; we detect+capture, never execute.

    Advertising it as core would imply execution support we deliberately do
    not have (spec §2 non-goals).
    """
    card = build_server_card()
    assert card.capabilities["tasks"] == {"extension": True}
    assert "tools" in card.capabilities
    assert "elicitation" in card.capabilities


def test_oidc_config_produces_an_oidc_auth_block() -> None:
    card = build_server_card(_Config(oidc_issuer="https://idp.example/oidc"))
    assert card.auth.type == "oidc"
    assert card.auth.issuer == "https://idp.example/oidc"


def test_insecure_server_reports_auth_none_rather_than_omitting_it() -> None:
    """Silence about absent auth invites a client to assume there is some."""
    card = build_server_card(_Config(insecure_no_auth=True))
    assert card.auth.type == "none"


def test_default_local_server_reports_bearer() -> None:
    """ADR-0184 makes local token auth the default; the card must say so."""
    assert build_server_card().auth.type == "bearer"


def test_endpoint_url_follows_the_configured_host_and_port() -> None:
    card = build_server_card(_Config(host="10.0.0.9", port=9999))
    assert card.endpoints[0].url == "http://10.0.0.9:9999/mcp"


def test_base_url_override_wins_for_public_deployments() -> None:
    card = build_server_card(_Config(host="127.0.0.1"), base_url="https://nova.example")
    assert card.endpoints[0].url == "https://nova.example/mcp"


def test_generated_card_validates_against_its_own_validator() -> None:
    """Generation and validation must agree, or one of them is wrong."""
    card = build_server_card()
    validate_server_card(card.model_dump(mode="json", exclude_none=True))


# ---------------------------------------------------------------------------
# Validation (R8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_required_field_is_enforced(field: str) -> None:
    doc = build_server_card().model_dump(mode="json", exclude_none=True)
    doc.pop(field)
    with pytest.raises(ServerCardValidationError, match=field):
        validate_server_card(doc)


def test_unknown_fields_are_forward_compatibility_not_errors() -> None:
    """SEP-1649 is evolving; an unrecognised key must not fail validation."""
    doc = build_server_card().model_dump(mode="json", exclude_none=True)
    doc["someFutureField"] = {"a": 1}
    validate_server_card(doc)  # must not raise


def test_oidc_without_an_issuer_is_rejected() -> None:
    doc = build_server_card().model_dump(mode="json", exclude_none=True)
    doc["auth"] = {"type": "oidc"}
    with pytest.raises(ServerCardValidationError, match="issuer"):
        validate_server_card(doc)


def test_non_object_document_is_rejected_clearly() -> None:
    with pytest.raises(ServerCardValidationError, match="JSON object"):
        validate_server_card([1, 2, 3])


def test_capabilities_must_be_an_object() -> None:
    doc = build_server_card().model_dump(mode="json", exclude_none=True)
    doc["capabilities"] = "tools"
    with pytest.raises(ServerCardValidationError, match="capabilities"):
        validate_server_card(doc)


# ---------------------------------------------------------------------------
# AC3 — a live GET returns a valid card
# ---------------------------------------------------------------------------


def test_serve_publishes_a_valid_card_unauthenticated(tmp_path) -> None:
    """AC3. Unauthenticated by design: a discovery document gated behind the
    auth it describes would be undiscoverable, which defeats its purpose."""
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from novafabric.server.app import create_app
    from novafabric.server.config import ServerConfig

    app = create_app(ServerConfig(db_path=str(tmp_path / "registry.db")))
    with TestClient(app) as client:
        response = client.get(WELL_KNOWN_PATH)  # no token supplied
    assert response.status_code == 200, response.text
    card = validate_server_card(json.loads(response.text))
    assert card.protocolVersion == "2026-07-28"
    assert fastapi  # silence unused
