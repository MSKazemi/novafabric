"""Tests for SAML 2.0 SSO server-mode slice (ADR-0138, experimental partial).

Covers:
* the additive ``server.saml`` config block (valid + invalid, closed schema);
* attribute → RBAC-role mapping (fail-closed, never escalates);
* the assertion validation policy (rules V3–V9, V11) over synthetic parsed
  assertions;
* the replay store (V8) TTL + bounds;
* the closed, redacted audit record (hygiene rule V-log);
* SP metadata XML emission + HTTP endpoint;
* the strict D5 refusal: login/ACS return 501 and never consume assertions;
* disabled-by-default: no ``saml`` block ⇒ 404 on all SAML endpoints;
* CLI ``nova server saml-metadata``.

No SAML library is used anywhere — ADR-0138 §D5 leaves it as an open
pre-adoption license gate; signature validation is never skipped or faked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from novafabric.cli.main import app as cli_app  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig, load_config  # noqa: E402
from novafabric.server.saml import (  # noqa: E402
    ALLOWED_SAML_ROLES,
    SAML_STATUS_SUCCESS,
    AssertionReplayStore,
    ParsedAssertion,
    SamlAuditRecord,
    SamlConfig,
    SamlValidationError,
    SamlVerifierUnavailableError,
    build_audit_record,
    hash_subject,
    map_attributes_to_roles,
    render_sp_metadata,
    require_signature_verifier,
    resolve_subject,
    validate_assertion_policy,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

SP_ENTITY_ID = "https://nova.example.org/saml/metadata"
ACS_URL = "https://nova.example.org/v0/auth/saml/acs"
IDP_ENTITY_ID = "https://idp.example.org"


def _config_dict(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True,
        "sp_entity_id": SP_ENTITY_ID,
        "acs_url": ACS_URL,
        "sp_cert_path": "/etc/novafabric/saml/sp.crt",
        "sp_key_path": "/etc/novafabric/saml/sp.key",
        "idp": {
            "entity_id": IDP_ENTITY_ID,
            "sso_url": "https://idp.example.org/sso",
            "x509_cert_path": "/etc/novafabric/saml/idp.crt",
        },
        "attribute_role_map": {
            "attribute": "groups",
            "mapping": {
                "nova-admins": ["admin"],
                "nova-writers": ["writer"],
                "nova-auditors": ["auditor", "reader"],
            },
            "default_roles": ["reader"],
        },
    }
    base.update(overrides)
    return base


@pytest.fixture
def saml_config() -> SamlConfig:
    return SamlConfig.model_validate(_config_dict())


def _assertion(**overrides: object) -> ParsedAssertion:
    now = datetime.now(timezone.utc)
    defaults: dict[str, object] = {
        "assertion_id": "_a1b2c3d4e5",
        "issuer": IDP_ENTITY_ID,
        "audiences": (SP_ENTITY_ID,),
        "name_id": "alice@example.com",
        "not_before": now - timedelta(minutes=1),
        "not_on_or_after": now + timedelta(minutes=5),
        "recipient": ACS_URL,
        "destination": ACS_URL,
        "in_response_to": "_req1",
        "status_code": SAML_STATUS_SUCCESS,
        "attributes": {"groups": ("nova-writers",)},
    }
    defaults.update(overrides)
    return ParsedAssertion(**defaults)  # type: ignore[arg-type]


def _validate(
    parsed: ParsedAssertion,
    config: SamlConfig,
    *,
    pending: set[str] | None = None,
    store: AssertionReplayStore | None = None,
    now: datetime | None = None,
) -> None:
    validate_assertion_policy(
        parsed,
        config,
        replay_store=store if store is not None else AssertionReplayStore(),
        pending_request_ids={"_req1"} if pending is None else pending,
        now=now,
    )


# ---------------------------------------------------------------------------
# Config model
# ---------------------------------------------------------------------------


class TestSamlConfig:
    def test_valid_full_config_parses(self, saml_config: SamlConfig) -> None:
        assert saml_config.enabled is True
        assert saml_config.clock_skew_seconds == 120  # default
        assert saml_config.allow_idp_initiated is False  # default
        assert saml_config.idp.entity_id == IDP_ENTITY_ID

    def test_valid_minimal_config(self) -> None:
        cfg = SamlConfig.model_validate(
            _config_dict(enabled=False, attribute_role_map=None)
        )
        assert cfg.enabled is False
        assert cfg.attribute_role_map is None

    def test_slo_block_accepted(self) -> None:
        raw = _config_dict()
        raw["idp"] = {**raw["idp"], "slo_url": "https://idp.example.org/slo"}  # type: ignore[dict-item]
        raw["slo"] = {"enabled": True, "slo_acs_url": "https://nova.example.org/v0/auth/saml/slo"}
        cfg = SamlConfig.model_validate(raw)
        assert cfg.slo is not None and cfg.slo.enabled

    def test_slo_enabled_requires_idp_slo_url(self) -> None:
        raw = _config_dict()
        raw["slo"] = {"enabled": True}
        with pytest.raises(ValidationError, match="idp.slo_url"):
            SamlConfig.model_validate(raw)

    def test_missing_enabled_rejected(self) -> None:
        raw = _config_dict()
        del raw["enabled"]
        with pytest.raises(ValidationError):
            SamlConfig.model_validate(raw)

    def test_missing_idp_rejected(self) -> None:
        raw = _config_dict()
        del raw["idp"]
        with pytest.raises(ValidationError):
            SamlConfig.model_validate(raw)

    def test_idp_missing_cert_rejected(self) -> None:
        raw = _config_dict()
        raw["idp"] = {"entity_id": IDP_ENTITY_ID, "sso_url": "https://idp.example.org/sso"}
        with pytest.raises(ValidationError):
            SamlConfig.model_validate(raw)

    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SamlConfig.model_validate(_config_dict(surprise="x"))

    def test_clock_skew_over_cap_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SamlConfig.model_validate(_config_dict(clock_skew_seconds=301))

    def test_bad_role_in_mapping_rejected(self) -> None:
        raw = _config_dict()
        raw["attribute_role_map"] = {
            "attribute": "groups",
            "mapping": {"g": ["superadmin"]},
        }
        with pytest.raises(ValidationError, match="superadmin"):
            SamlConfig.model_validate(raw)

    def test_bad_role_in_default_roles_rejected(self) -> None:
        raw = _config_dict()
        raw["attribute_role_map"] = {
            "attribute": "groups",
            "mapping": {},
            "default_roles": ["root"],
        }
        with pytest.raises(ValidationError, match="root"):
            SamlConfig.model_validate(raw)

    def test_non_http_acs_url_rejected(self) -> None:
        with pytest.raises(ValidationError, match="acs_url"):
            SamlConfig.model_validate(_config_dict(acs_url="ftp://x"))

    def test_non_http_sso_url_rejected(self) -> None:
        raw = _config_dict()
        raw["idp"] = {**raw["idp"], "sso_url": "not-a-url"}  # type: ignore[dict-item]
        with pytest.raises(ValidationError, match="sso_url"):
            SamlConfig.model_validate(raw)

    def test_promoter_approver_roles_allowed(self) -> None:
        raw = _config_dict()
        raw["attribute_role_map"] = {
            "attribute": "groups",
            "mapping": {"release-managers": ["promoter", "approver"]},
        }
        cfg = SamlConfig.model_validate(raw)
        assert cfg.attribute_role_map is not None

    def test_server_config_saml_absent_by_default(self) -> None:
        cfg = ServerConfig()
        assert cfg.saml is None

    def test_server_config_loads_saml_block_from_yaml(self, tmp_path: Path) -> None:
        path = tmp_path / "server.yaml"
        path.write_text(yaml.safe_dump({"saml": _config_dict()}))
        cfg = load_config(path)
        assert cfg.saml is not None and cfg.saml.enabled


# ---------------------------------------------------------------------------
# Attribute → role mapping
# ---------------------------------------------------------------------------


class TestRoleMapping:
    def test_union_of_matched_values(self, saml_config: SamlConfig) -> None:
        rm = saml_config.attribute_role_map
        assert rm is not None
        roles = map_attributes_to_roles(
            rm, {"groups": ("nova-writers", "nova-auditors")}
        )
        assert roles == ["auditor", "reader", "writer"]

    def test_unmapped_value_fails_closed_to_default(self, saml_config: SamlConfig) -> None:
        rm = saml_config.attribute_role_map
        assert rm is not None
        assert map_attributes_to_roles(rm, {"groups": ("strangers",)}) == ["reader"]

    def test_no_default_roles_means_no_roles(self) -> None:
        raw = _config_dict()
        raw["attribute_role_map"] = {"attribute": "groups", "mapping": {}}
        rm = SamlConfig.model_validate(raw).attribute_role_map
        assert rm is not None
        assert map_attributes_to_roles(rm, {"groups": ("anything",)}) == []
        assert map_attributes_to_roles(rm, {}) == []

    def test_attribute_absent_grants_default_only(self, saml_config: SamlConfig) -> None:
        rm = saml_config.attribute_role_map
        assert rm is not None
        assert map_attributes_to_roles(rm, {"other": ("nova-admins",)}) == ["reader"]

    def test_roles_deduplicated(self, saml_config: SamlConfig) -> None:
        rm = saml_config.attribute_role_map
        assert rm is not None
        roles = map_attributes_to_roles(
            rm, {"groups": ("nova-auditors", "nova-auditors")}
        )
        assert roles == ["auditor", "reader"]


class TestResolveSubject:
    def test_name_id_default(self, saml_config: SamlConfig) -> None:
        cfg = SamlConfig.model_validate(_config_dict())
        assert resolve_subject(_assertion(), cfg) == "alice@example.com"

    def test_subject_attribute_override(self) -> None:
        cfg = SamlConfig.model_validate(_config_dict(subject_attribute="email"))
        parsed = _assertion(attributes={"email": ("a@example.org",)})
        assert resolve_subject(parsed, cfg) == "a@example.org"

    def test_missing_subject_attribute_rejected(self) -> None:
        cfg = SamlConfig.model_validate(_config_dict(subject_attribute="email"))
        with pytest.raises(SamlValidationError) as exc:
            resolve_subject(_assertion(attributes={}), cfg)
        assert exc.value.reason == "subject_unresolved"

    def test_ambiguous_subject_attribute_rejected(self) -> None:
        cfg = SamlConfig.model_validate(_config_dict(subject_attribute="email"))
        parsed = _assertion(attributes={"email": ("a@x", "b@x")})
        with pytest.raises(SamlValidationError):
            resolve_subject(parsed, cfg)

    def test_empty_name_id_rejected(self, saml_config: SamlConfig) -> None:
        with pytest.raises(SamlValidationError):
            resolve_subject(_assertion(name_id=""), saml_config)


# ---------------------------------------------------------------------------
# Validation policy (V3–V9, V11)
# ---------------------------------------------------------------------------


class TestValidationPolicy:
    def test_valid_assertion_passes(self, saml_config: SamlConfig) -> None:
        _validate(_assertion(), saml_config)  # must not raise

    def test_v11_status_not_success_rejected(self, saml_config: SamlConfig) -> None:
        bad = _assertion(status_code="urn:oasis:names:tc:SAML:2.0:status:Requester")
        with pytest.raises(SamlValidationError) as exc:
            _validate(bad, saml_config)
        assert exc.value.reason == "status_not_success"

    def test_v3_issuer_mismatch_rejected(self, saml_config: SamlConfig) -> None:
        with pytest.raises(SamlValidationError) as exc:
            _validate(_assertion(issuer="https://evil.example.org"), saml_config)
        assert exc.value.reason == "issuer_mismatch"

    def test_v4_audience_mismatch_rejected(self, saml_config: SamlConfig) -> None:
        with pytest.raises(SamlValidationError) as exc:
            _validate(_assertion(audiences=("https://other-sp.example",)), saml_config)
        assert exc.value.reason == "audience_mismatch"

    def test_v5_not_yet_valid_rejected(self, saml_config: SamlConfig) -> None:
        now = datetime.now(timezone.utc)
        bad = _assertion(
            not_before=now + timedelta(minutes=10),
            not_on_or_after=now + timedelta(minutes=15),
        )
        with pytest.raises(SamlValidationError) as exc:
            _validate(bad, saml_config, now=now)
        assert exc.value.reason == "not_yet_valid"

    def test_v5_not_before_within_skew_accepted(self, saml_config: SamlConfig) -> None:
        now = datetime.now(timezone.utc)
        ok = _assertion(not_before=now + timedelta(seconds=60))  # inside 120 s skew
        _validate(ok, saml_config, now=now)

    def test_v6_expired_rejected(self, saml_config: SamlConfig) -> None:
        now = datetime.now(timezone.utc)
        bad = _assertion(
            not_before=now - timedelta(minutes=30),
            not_on_or_after=now - timedelta(minutes=10),
        )
        with pytest.raises(SamlValidationError) as exc:
            _validate(bad, saml_config, now=now)
        assert exc.value.reason == "expired"

    def test_v6_expired_within_skew_accepted(self, saml_config: SamlConfig) -> None:
        now = datetime.now(timezone.utc)
        ok = _assertion(not_on_or_after=now - timedelta(seconds=60))  # inside skew
        _validate(ok, saml_config, now=now)

    def test_v7_recipient_mismatch_rejected(self, saml_config: SamlConfig) -> None:
        with pytest.raises(SamlValidationError) as exc:
            _validate(_assertion(recipient="https://evil.example/acs"), saml_config)
        assert exc.value.reason == "recipient_mismatch"

    def test_v7_destination_mismatch_rejected(self, saml_config: SamlConfig) -> None:
        with pytest.raises(SamlValidationError) as exc:
            _validate(_assertion(destination="https://evil.example/acs"), saml_config)
        assert exc.value.reason == "recipient_mismatch"

    def test_v9_in_response_to_mismatch_rejected(self, saml_config: SamlConfig) -> None:
        with pytest.raises(SamlValidationError) as exc:
            _validate(_assertion(in_response_to="_unknown"), saml_config)
        assert exc.value.reason == "in_response_to_mismatch"

    def test_v9_idp_initiated_rejected_by_default(self, saml_config: SamlConfig) -> None:
        with pytest.raises(SamlValidationError) as exc:
            _validate(_assertion(in_response_to=None), saml_config)
        assert exc.value.reason == "idp_initiated_disabled"

    def test_v9_idp_initiated_allowed_when_opted_in(self) -> None:
        cfg = SamlConfig.model_validate(_config_dict(allow_idp_initiated=True))
        _validate(_assertion(in_response_to=None), cfg)

    def test_v8_replayed_assertion_id_rejected(self, saml_config: SamlConfig) -> None:
        store = AssertionReplayStore()
        _validate(_assertion(), saml_config, store=store)
        with pytest.raises(SamlValidationError) as exc:
            _validate(_assertion(), saml_config, store=store)
        assert exc.value.reason == "replayed"

    def test_rejected_assertion_does_not_consume_replay_slot(
        self, saml_config: SamlConfig
    ) -> None:
        store = AssertionReplayStore()
        with pytest.raises(SamlValidationError):
            _validate(_assertion(issuer="https://evil.example"), saml_config, store=store)
        # Same ID with a valid assertion must still be accepted afterwards.
        _validate(_assertion(), saml_config, store=store)


class TestReplayStore:
    def test_ttl_eviction(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = [1000.0]
        monkeypatch.setattr("novafabric.server.saml.time.monotonic", lambda: clock[0])
        store = AssertionReplayStore(ttl_seconds=10)
        assert store.check_and_record("_x") is True
        assert store.check_and_record("_x") is False
        clock[0] += 11
        assert store.check_and_record("_x") is True  # expired → accepted again

    def test_bounded_size(self) -> None:
        store = AssertionReplayStore(ttl_seconds=3600, max_entries=5)
        for i in range(20):
            assert store.check_and_record(f"_id{i}") is True
        assert len(store) <= 6  # max + the just-inserted entry

    def test_invalid_ttl_rejected(self) -> None:
        with pytest.raises(ValueError):
            AssertionReplayStore(ttl_seconds=0)


# ---------------------------------------------------------------------------
# Redacted audit record
# ---------------------------------------------------------------------------


class TestAuditRecord:
    def test_accepted_record(self) -> None:
        rec = build_audit_record(
            accepted=True,
            assertion_id="_a1",
            idp_entity_id=IDP_ENTITY_ID,
            subject="alice@example.com",
            mapped_roles=["writer"],
            idp_initiated=False,
        )
        assert rec.event.value == "saml_assertion_accepted"
        assert rec.sub_hash == hash_subject("alice@example.com")
        assert rec.sub_hash.startswith("sha256:")
        assert "alice" not in rec.model_dump_json()

    def test_rejected_record_carries_reason(self) -> None:
        rec = build_audit_record(
            accepted=False,
            assertion_id="_a2",
            idp_entity_id=IDP_ENTITY_ID,
            subject="bob@example.com",
            mapped_roles=[],
            idp_initiated=True,
            reject_reason="expired",
        )
        assert rec.event.value == "saml_assertion_rejected"
        assert rec.reject_reason == "expired"

    def test_closed_schema_rejects_name_id(self) -> None:
        with pytest.raises(ValidationError):
            SamlAuditRecord.model_validate(
                {
                    "event": "saml_assertion_accepted",
                    "assertion_id": "_a1",
                    "idp_entity_id": IDP_ENTITY_ID,
                    "sub_hash": hash_subject("x"),
                    "mapped_roles": [],
                    "idp_initiated": False,
                    "received_at": "2026-07-15T00:00:00+00:00",
                    "name_id": "alice@example.com",  # forbidden
                }
            )

    def test_bad_sub_hash_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SamlAuditRecord.model_validate(
                {
                    "event": "saml_assertion_accepted",
                    "assertion_id": "_a1",
                    "idp_entity_id": IDP_ENTITY_ID,
                    "sub_hash": "md5:abc",
                    "mapped_roles": [],
                    "idp_initiated": False,
                    "received_at": "2026-07-15T00:00:00+00:00",
                }
            )

    def test_bad_role_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SamlAuditRecord.model_validate(
                {
                    "event": "saml_assertion_accepted",
                    "assertion_id": "_a1",
                    "idp_entity_id": IDP_ENTITY_ID,
                    "sub_hash": hash_subject("x"),
                    "mapped_roles": ["superadmin"],
                    "idp_initiated": False,
                    "received_at": "2026-07-15T00:00:00+00:00",
                }
            )

    def test_bad_event_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SamlAuditRecord.model_validate(
                {
                    "event": "saml_login",
                    "assertion_id": "_a1",
                    "idp_entity_id": IDP_ENTITY_ID,
                    "sub_hash": hash_subject("x"),
                    "mapped_roles": [],
                    "idp_initiated": False,
                    "received_at": "2026-07-15T00:00:00+00:00",
                }
            )


# ---------------------------------------------------------------------------
# SP metadata emitter
# ---------------------------------------------------------------------------

_MD = "{urn:oasis:names:tc:SAML:2.0:metadata}"
_DS = "{http://www.w3.org/2000/09/xmldsig#}"

_TEST_CERT_PEM = (
    "-----BEGIN CERTIFICATE-----\n"
    "TUlJQ2R1bW15Y2VydGJvZHlmb3J0ZXN0aW5nb25seQ==\n"
    "-----END CERTIFICATE-----\n"
)


class TestSpMetadata:
    def test_metadata_structure(self, saml_config: SamlConfig) -> None:
        xml = render_sp_metadata(saml_config)
        root = ET.fromstring(xml)  # our own generated XML — safe to parse
        assert root.tag == f"{_MD}EntityDescriptor"
        assert root.attrib["entityID"] == SP_ENTITY_ID
        spsso = root.find(f"{_MD}SPSSODescriptor")
        assert spsso is not None
        assert spsso.attrib["WantAssertionsSigned"] == "true"
        acs = spsso.find(f"{_MD}AssertionConsumerService")
        assert acs is not None
        assert acs.attrib["Location"] == ACS_URL
        assert acs.attrib["Binding"].endswith("HTTP-POST")

    def test_metadata_embeds_sp_cert_when_present(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "sp.crt"
        cert_path.write_text(_TEST_CERT_PEM)
        cfg = SamlConfig.model_validate(_config_dict(sp_cert_path=str(cert_path)))
        root = ET.fromstring(render_sp_metadata(cfg))
        cert_el = root.find(f".//{_DS}X509Certificate")
        assert cert_el is not None
        assert cert_el.text == "TUlJQ2R1bW15Y2VydGJvZHlmb3J0ZXN0aW5nb25seQ=="

    def test_metadata_omits_cert_when_unreadable(self, saml_config: SamlConfig) -> None:
        # sp_cert_path points at a non-existent file → no KeyDescriptor, still valid.
        root = ET.fromstring(render_sp_metadata(saml_config))
        assert root.find(f".//{_DS}X509Certificate") is None

    def test_metadata_omits_cert_on_garbage_pem(self, tmp_path: Path) -> None:
        cert_path = tmp_path / "sp.crt"
        cert_path.write_text(
            "-----BEGIN CERTIFICATE-----\nnot!base64@@\n-----END CERTIFICATE-----\n"
        )
        cfg = SamlConfig.model_validate(_config_dict(sp_cert_path=str(cert_path)))
        root = ET.fromstring(render_sp_metadata(cfg))
        assert root.find(f".//{_DS}X509Certificate") is None

    def test_metadata_includes_slo_when_enabled(self) -> None:
        raw = _config_dict()
        raw["idp"] = {**raw["idp"], "slo_url": "https://idp.example.org/slo"}  # type: ignore[dict-item]
        raw["slo"] = {"enabled": True, "slo_acs_url": "https://nova.example.org/v0/auth/saml/slo"}
        cfg = SamlConfig.model_validate(raw)
        root = ET.fromstring(render_sp_metadata(cfg))
        slo = root.find(f".//{_MD}SingleLogoutService")
        assert slo is not None
        assert slo.attrib["Location"] == "https://nova.example.org/v0/auth/saml/slo"


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


def _client(tmp_path: Path, saml: dict[str, object] | None) -> TestClient:
    cfg = ServerConfig.model_validate(
        {"db_path": str(tmp_path / "test.db"), **({"saml": saml} if saml else {})}
    )
    return TestClient(create_app(cfg), raise_server_exceptions=False)


class TestSamlEndpointsDisabled:
    """No ``saml`` block (or enabled=false) ⇒ 404 everywhere — identical to today."""

    def test_metadata_404_without_config(self, tmp_path: Path) -> None:
        client = _client(tmp_path, None)
        assert client.get("/v0/auth/saml/metadata").status_code == 404

    def test_acs_404_without_config(self, tmp_path: Path) -> None:
        client = _client(tmp_path, None)
        assert client.post("/v0/auth/saml/acs", content=b"<xml/>").status_code == 404

    def test_login_404_without_config(self, tmp_path: Path) -> None:
        client = _client(tmp_path, None)
        assert client.get("/v0/auth/saml/login").status_code == 404

    def test_metadata_404_when_disabled(self, tmp_path: Path) -> None:
        client = _client(tmp_path, _config_dict(enabled=False))
        assert client.get("/v0/auth/saml/metadata").status_code == 404

    def test_health_unaffected(self, tmp_path: Path) -> None:
        client = _client(tmp_path, None)
        resp = client.get("/health")
        assert resp.status_code == 200 and resp.json()["ok"] is True


class TestSamlEndpointsEnabled:
    def test_metadata_endpoint_serves_xml(self, tmp_path: Path) -> None:
        client = _client(tmp_path, _config_dict())
        resp = client.get("/v0/auth/saml/metadata")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/samlmetadata+xml")
        root = ET.fromstring(resp.text)
        assert root.attrib["entityID"] == SP_ENTITY_ID

    def test_acs_refuses_with_501_never_consumes(self, tmp_path: Path) -> None:
        client = _client(tmp_path, _config_dict())
        resp = client.post(
            "/v0/auth/saml/acs",
            data={"SAMLResponse": "PHNhbWw6QXNzZXJ0aW9uLz4="},
        )
        assert resp.status_code == 501
        err = resp.json()["error"]
        assert err["code"] == "saml_not_available"
        assert "ADR-0138" in err["message"] and "D5" in err["message"]

    def test_login_refuses_with_501(self, tmp_path: Path) -> None:
        client = _client(tmp_path, _config_dict())
        resp = client.get("/v0/auth/saml/login")
        assert resp.status_code == 501
        assert resp.json()["error"]["code"] == "saml_not_available"


class TestD5Gate:
    def test_require_signature_verifier_always_raises_today(self) -> None:
        with pytest.raises(SamlVerifierUnavailableError, match="ADR-0138 D5"):
            require_signature_verifier()

    def test_allowed_roles_are_the_six_existing_ones(self) -> None:
        assert ALLOWED_SAML_ROLES == {
            "reader", "writer", "admin", "auditor", "promoter", "approver",
        }


# ---------------------------------------------------------------------------
# CLI — nova server saml-metadata
# ---------------------------------------------------------------------------

runner = CliRunner()


class TestSamlMetadataCli:
    def test_unconfigured_exits_nonzero(self, tmp_path: Path) -> None:
        path = tmp_path / "server.yaml"
        path.write_text(yaml.safe_dump({"host": "127.0.0.1"}))
        result = runner.invoke(cli_app, ["server", "saml-metadata", "--config", str(path)])
        assert result.exit_code == 1
        assert "not configured" in result.output

    def test_configured_prints_metadata_xml(self, tmp_path: Path) -> None:
        path = tmp_path / "server.yaml"
        path.write_text(yaml.safe_dump({"saml": _config_dict()}))
        result = runner.invoke(cli_app, ["server", "saml-metadata", "--config", str(path)])
        assert result.exit_code == 0, result.output
        root = ET.fromstring(result.output)
        assert root.attrib["entityID"] == SP_ENTITY_ID

    def test_disabled_block_exits_nonzero(self, tmp_path: Path) -> None:
        path = tmp_path / "server.yaml"
        path.write_text(yaml.safe_dump({"saml": _config_dict(enabled=False)}))
        result = runner.invoke(cli_app, ["server", "saml-metadata", "--config", str(path)])
        assert result.exit_code == 1
