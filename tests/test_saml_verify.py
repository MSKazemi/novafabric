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
"""SAML XML-DSIG verifier (ADR-0138 V1/V2/V10) — signxml-backed.

Signs real SAML responses with a throwaway test IdP key and asserts the verifier
accepts a valid signature, extracts the right identity, and rejects tampered,
unsigned, wrong-key, and XXE-bearing documents.
"""

from __future__ import annotations

import datetime

import pytest

pytest.importorskip("signxml")
pytest.importorskip("lxml")

from novafabric.server.saml import SamlValidationError  # noqa: E402
from novafabric.server.saml_verify import (  # noqa: E402
    parse_and_verify_response,
    signature_verifier_available,
)


def _make_idp_keypair():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-idp")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
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
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    return key_pem, cert_pem


_RESPONSE_TEMPLATE = """<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
  xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion"
  ID="_resp1" Version="2.0" Destination="https://sp.example.com/acs"
  IssueInstant="2026-07-25T00:00:00Z">
  <saml:Issuer>https://idp.example.com</saml:Issuer>
  <samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>
  <saml:Assertion ID="_assert1" Version="2.0" IssueInstant="2026-07-25T00:00:00Z">
    <saml:Issuer>https://idp.example.com</saml:Issuer>
    <saml:Subject>
      <saml:NameID>alice@example.com</saml:NameID>
      <saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer">
        <saml:SubjectConfirmationData Recipient="https://sp.example.com/acs" InResponseTo="_req1"/>
      </saml:SubjectConfirmation>
    </saml:Subject>
    <saml:Conditions NotBefore="2026-07-25T00:00:00Z" NotOnOrAfter="2099-01-01T00:00:00Z">
      <saml:AudienceRestriction><saml:Audience>https://sp.example.com</saml:Audience></saml:AudienceRestriction>
    </saml:Conditions>
    <saml:AttributeStatement>
      <saml:Attribute Name="groups">
        <saml:AttributeValue>admins</saml:AttributeValue>
        <saml:AttributeValue>eng</saml:AttributeValue>
      </saml:Attribute>
    </saml:AttributeStatement>
  </saml:Assertion>
</samlp:Response>"""


def _signed_response(key_pem: bytes, cert_pem: bytes) -> bytes:
    import lxml.etree as ET
    from signxml import XMLSigner

    root = ET.fromstring(_RESPONSE_TEMPLATE.encode())
    signed = XMLSigner().sign(root, key=key_pem, cert=cert_pem)
    return ET.tostring(signed)


def test_verifier_available() -> None:
    assert signature_verifier_available() is True


def test_valid_signature_extracts_identity() -> None:
    key_pem, cert_pem = _make_idp_keypair()
    xml = _signed_response(key_pem, cert_pem)

    parsed = parse_and_verify_response(xml, idp_cert_pem=cert_pem.decode())

    assert parsed.name_id == "alice@example.com"
    assert parsed.issuer == "https://idp.example.com"
    assert parsed.audiences == ("https://sp.example.com",)
    assert parsed.recipient == "https://sp.example.com/acs"
    assert parsed.in_response_to == "_req1"
    assert parsed.destination == "https://sp.example.com/acs"
    assert parsed.status_code == "urn:oasis:names:tc:SAML:2.0:status:Success"
    assert parsed.attributes["groups"] == ("admins", "eng")


def test_tampered_assertion_is_rejected() -> None:
    key_pem, cert_pem = _make_idp_keypair()
    xml = _signed_response(key_pem, cert_pem)
    tampered = xml.replace(b"alice@example.com", b"attacker@evil.com")

    with pytest.raises(SamlValidationError):
        parse_and_verify_response(tampered, idp_cert_pem=cert_pem.decode())


def test_unsigned_response_is_rejected() -> None:
    _, cert_pem = _make_idp_keypair()
    with pytest.raises(SamlValidationError):
        parse_and_verify_response(_RESPONSE_TEMPLATE.encode(), idp_cert_pem=cert_pem.decode())


def test_wrong_cert_is_rejected() -> None:
    key_pem, _ = _make_idp_keypair()
    xml = _signed_response(key_pem, _make_idp_keypair()[1])  # sign with A, verify with B's cert
    _, other_cert = _make_idp_keypair()
    with pytest.raises(SamlValidationError):
        parse_and_verify_response(xml, idp_cert_pem=other_cert.decode())


def test_doctype_is_rejected_before_parsing() -> None:
    key_pem, cert_pem = _make_idp_keypair()
    xml = _signed_response(key_pem, cert_pem)
    xxe = b'<?xml version="1.0"?>\n<!DOCTYPE foo [<!ENTITY x "y">]>\n' + xml.split(b"?>", 1)[-1]
    with pytest.raises(SamlValidationError) as ei:
        parse_and_verify_response(xxe, idp_cert_pem=cert_pem.decode())
    assert ei.value.reason == "xxe_doctype_rejected"
