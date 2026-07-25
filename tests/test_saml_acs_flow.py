# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""End-to-end SAML ACS flow (ADR-0138, experimental opt-in).

Drives the whole route: a signed IdP-initiated Response → XML-DSIG verify →
policy (V3–V9, V11) → role mapping → bearer token. Also pins the safe default:
without the ``experimental_acs_enabled`` opt-in the ACS still refuses with 501
and never consumes the assertion.
"""

from __future__ import annotations

import base64
import datetime
from pathlib import Path

import pytest

pytest.importorskip("signxml")
pytest.importorskip("lxml")

from fastapi.testclient import TestClient  # noqa: E402

from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.config import ServerConfig  # noqa: E402

SP_ENTITY_ID = "https://nova.example.org/saml/metadata"
ACS_URL = "https://nova.example.org/v0/auth/saml/acs"
IDP_ENTITY_ID = "https://idp.example.org"


def _idp_keypair():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "idp")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return key_pem, cert.public_bytes(serialization.Encoding.PEM)


def _signed_saml_response(key_pem: bytes) -> bytes:
    import lxml.etree as ET
    from signxml import XMLSigner

    now = datetime.datetime.now(datetime.timezone.utc)
    nb = (now - datetime.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    na = (now + datetime.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ii = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    xml = f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
      xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
      ID="_resp1" Version="2.0" Destination="{ACS_URL}" IssueInstant="{ii}">
      <saml:Issuer>{IDP_ENTITY_ID}</saml:Issuer>
      <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
      <saml:Assertion ID="_assert1" Version="2.0" IssueInstant="{ii}">
        <saml:Issuer>{IDP_ENTITY_ID}</saml:Issuer>
        <saml:Subject>
          <saml:NameID>alice@example.com</saml:NameID>
          <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
            <saml:SubjectConfirmationData Recipient="{ACS_URL}"/>
          </saml:SubjectConfirmation>
        </saml:Subject>
        <saml:Conditions NotBefore="{nb}" NotOnOrAfter="{na}">
          <saml:AudienceRestriction><saml:Audience>{SP_ENTITY_ID}</saml:Audience></saml:AudienceRestriction>
        </saml:Conditions>
        <saml:AttributeStatement>
          <saml:Attribute Name="groups"><saml:AttributeValue>nova-writers</saml:AttributeValue></saml:Attribute>
        </saml:AttributeStatement>
      </saml:Assertion>
    </samlp:Response>"""
    root = ET.fromstring(xml.encode())
    signed = XMLSigner().sign(root, key=key_pem, cert=None, reference_uri="_assert1")
    return ET.tostring(signed)


def _config(cert_path: Path, *, experimental: bool) -> dict:
    return {
        "enabled": True,
        "sp_entity_id": SP_ENTITY_ID,
        "acs_url": ACS_URL,
        "sp_cert_path": "/tmp/sp.crt",
        "sp_key_path": "/tmp/sp.key",
        "idp": {
            "entity_id": IDP_ENTITY_ID,
            "sso_url": "https://idp.example.org/sso",
            "x509_cert_path": str(cert_path),
        },
        "attribute_role_map": {
            "attribute": "groups",
            "mapping": {"nova-writers": ["writer"]},
            "default_roles": ["reader"],
        },
        "allow_idp_initiated": True,
        "experimental_acs_enabled": experimental,
    }


def _client(tmp_path: Path, saml: dict) -> TestClient:
    cfg = ServerConfig.model_validate({"db_path": str(tmp_path / "t.db"), "saml": saml})
    return TestClient(create_app(cfg), raise_server_exceptions=False)


def test_acs_still_501_without_opt_in(tmp_path: Path) -> None:
    _, cert_pem = _idp_keypair()
    cert_path = tmp_path / "idp.crt"
    cert_path.write_bytes(cert_pem)
    client = _client(tmp_path, _config(cert_path, experimental=False))
    resp = client.post("/v0/auth/saml/acs", data={"SAMLResponse": "PGE+PC9hPg=="})
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "saml_not_available"


def test_acs_accepts_signed_assertion_and_mints_token(tmp_path: Path) -> None:
    key_pem, cert_pem = _idp_keypair()
    cert_path = tmp_path / "idp.crt"
    cert_path.write_bytes(cert_pem)
    client = _client(tmp_path, _config(cert_path, experimental=True))

    saml_response = base64.b64encode(_signed_saml_response(key_pem)).decode()
    resp = client.post("/v0/auth/saml/acs", data={"SAMLResponse": saml_response})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["token_type"] == "Bearer"
    assert body["roles"] == ["writer"]
    assert body["access_token"]


def test_acs_rejects_tampered_assertion(tmp_path: Path) -> None:
    key_pem, cert_pem = _idp_keypair()
    cert_path = tmp_path / "idp.crt"
    cert_path.write_bytes(cert_pem)
    client = _client(tmp_path, _config(cert_path, experimental=True))

    signed = _signed_saml_response(key_pem).replace(b"alice@example.com", b"attacker@evil.com")
    resp = client.post(
        "/v0/auth/saml/acs", data={"SAMLResponse": base64.b64encode(signed).decode()}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "saml_assertion_rejected"


def test_login_redirects_when_enabled(tmp_path: Path) -> None:
    _, cert_pem = _idp_keypair()
    cert_path = tmp_path / "idp.crt"
    cert_path.write_bytes(cert_pem)
    client = _client(tmp_path, _config(cert_path, experimental=True))
    resp = client.get("/v0/auth/saml/login", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("https://idp.example.org/sso?SAMLRequest=")
