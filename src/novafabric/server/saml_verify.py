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
"""SAML assertion XML layer — the V1/V2/V10 verifier the policy layer waited for.

This is the ADR-0138 §D5 piece that was deliberately absent: XXE-hardened XML
parsing (V10) plus XML-DSIG signature verification (V1/V2), producing the
:class:`~novafabric.server.saml.ParsedAssertion` the existing policy layer
(rules V3–V9, V11) already consumes. It is built on **signxml** (Apache-2.0) and
**lxml** (BSD-3-Clause) — the transitive tree that finally clears the ADR-0024
license gate the §D5 note describes.

Security posture (why this is safe to consume untrusted XML):

* **XXE (V10):** the parser resolves no entities, loads no DTD, and reaches no
  network; a document carrying a ``<!DOCTYPE`` / ``<!ENTITY`` is rejected before
  parsing rather than parsed defensively.
* **XSW / signature wrapping (V1/V2):** identity is extracted **only** from the
  element signxml returns as signature-verified (``VerifyResult.signed_xml``),
  never from the surrounding (attacker-controllable) document. A response whose
  signature is absent, does not verify, or is signed by a different key is
  rejected — the verifier never returns a partially-trusted view.

Status: **experimental** (ADR-0138). A Security-Architect review remains a
pre-production blocking condition (CLAUDE.md) before the ACS route is enabled in
a production deployment.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from novafabric.server.saml import ParsedAssertion, SamlValidationError

_SAML_NS = "urn:oasis:names:tc:SAML:2.0:assertion"
_PROTO_NS = "urn:oasis:names:tc:SAML:2.0:protocol"
_NSMAP = {"saml": _SAML_NS, "samlp": _PROTO_NS}


def signature_verifier_available() -> bool:
    """Return True when signxml + lxml are importable (the ADR-0138 D5 library)."""
    try:
        import lxml.etree  # noqa: F401
        import signxml  # noqa: F401
    except ImportError:
        return False
    return True


def _reject_doctype(xml: bytes) -> None:
    # V10: refuse any DOCTYPE/ENTITY outright rather than trusting parser flags alone.
    head = xml[:4096].lower()
    if b"<!doctype" in head or b"<!entity" in head:
        raise SamlValidationError(
            "xxe_doctype_rejected",
            "assertion XML carries a DOCTYPE/ENTITY declaration and is rejected (V10)",
        )


def _hardened_parse(xml: bytes) -> Any:
    import lxml.etree as ET

    parser = ET.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
    )
    try:
        return ET.fromstring(xml, parser=parser)
    except ET.XMLSyntaxError as exc:
        raise SamlValidationError("xml_malformed", "assertion XML did not parse") from exc


def _text(el: Any, path: str) -> str:
    found = el.find(path, _NSMAP)
    return (found.text or "").strip() if found is not None else ""


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    from datetime import timezone

    v = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _extract(assertion: Any, response_root: Any) -> ParsedAssertion:
    import lxml.etree as ET

    conditions = assertion.find("saml:Conditions", _NSMAP)
    audiences: tuple[str, ...] = ()
    not_before = not_after = None
    if conditions is not None:
        not_before = _parse_dt(conditions.get("NotBefore"))
        not_after = _parse_dt(conditions.get("NotOnOrAfter"))
        audiences = tuple(
            (a.text or "").strip()
            for a in conditions.findall("saml:AudienceRestriction/saml:Audience", _NSMAP)
            if (a.text or "").strip()
        )

    scd = assertion.find(
        "saml:Subject/saml:SubjectConfirmation/saml:SubjectConfirmationData", _NSMAP
    )
    recipient = scd.get("Recipient") if scd is not None else None
    in_response_to = scd.get("InResponseTo") if scd is not None else None

    attributes: dict[str, tuple[str, ...]] = {}
    for attr in assertion.findall("saml:AttributeStatement/saml:Attribute", _NSMAP):
        name = attr.get("Name") or attr.get("FriendlyName") or ""
        vals = tuple(
            (v.text or "").strip()
            for v in attr.findall("saml:AttributeValue", _NSMAP)
            if (v.text or "").strip()
        )
        if name:
            attributes[name] = vals

    status_code = ""
    destination = None
    if response_root is not None and ET.QName(response_root).localname == "Response":
        destination = response_root.get("Destination")
        sc = response_root.find("samlp:Status/samlp:StatusCode", _NSMAP)
        if sc is not None:
            status_code = sc.get("Value") or ""

    return ParsedAssertion(
        assertion_id=assertion.get("ID") or "",
        issuer=_text(assertion, "saml:Issuer"),
        audiences=audiences,
        name_id=_text(assertion, "saml:Subject/saml:NameID"),
        not_before=not_before,
        not_on_or_after=not_after,
        recipient=recipient,
        destination=destination,
        in_response_to=in_response_to,
        status_code=status_code,
        attributes=attributes,
    )


def parse_and_verify_response(response_xml: bytes, *, idp_cert_pem: str) -> ParsedAssertion:
    """Verify a SAML Response's XML-DSIG signature and return a :class:`ParsedAssertion`.

    Args:
        response_xml: the raw ``<samlp:Response>`` bytes (HTTP-POST binding body,
            already base64-decoded by the caller).
        idp_cert_pem: the IdP's X.509 signing certificate in PEM form.

    Raises:
        SamlValidationError: on a DOCTYPE/ENTITY (V10), a malformed document, or a
            missing / invalid / wrong-key signature (V1/V2). The identity view is
            built **only** from the signxml-verified element (XSW defense).
    """
    import lxml.etree as ET
    from signxml import XMLVerifier  # type: ignore[attr-defined]

    _reject_doctype(response_xml)
    root = _hardened_parse(response_xml)

    try:
        result = XMLVerifier().verify(root, x509_cert=idp_cert_pem)
    except Exception as exc:  # signxml raises InvalidSignature/InvalidCertificate/etc.
        raise SamlValidationError(
            "signature_invalid",
            "SAML assertion signature is absent, invalid, or signed by an unknown key (V1/V2)",
        ) from exc

    # verify() returns a single VerifyResult (one signature) or a list (many).
    if isinstance(result, list):
        result = result[0]
    signed = result.signed_xml  # the verified element — the ONLY trusted view
    if signed is None:
        raise SamlValidationError("signature_invalid", "no signed element was returned (V1/V2)")

    # The signature may cover the Assertion or the whole Response.
    if ET.QName(signed).localname == "Assertion":
        assertion = signed
    else:
        assertion = signed.find("saml:Assertion", _NSMAP)
    if assertion is None:
        raise SamlValidationError(
            "assertion_unsigned",
            "the verified signature does not cover a SAML Assertion (XSW defense)",
        )

    return _extract(assertion, root if ET.QName(root).localname == "Response" else None)
