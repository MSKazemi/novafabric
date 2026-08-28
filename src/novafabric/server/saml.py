"""SAML 2.0 SSO (SP role) for server mode — ADR-0138, experimental partial slice.

Implements the parts of ADR-0138 / ``the private design/spec/saml-sso-v0.md`` that do NOT
require the gated SAML library (ADR-0138 §D5 leaves the XML-signature library
as an open pre-adoption license gate — "this ADR authorizes the design, not
the dependency"):

* the additive ``server.saml`` config block (Pydantic mirror of
  ``server-saml-config.schema.json``);
* the SP metadata XML emitter (``nova server saml-metadata`` +
  ``GET /v0/auth/saml/metadata``) — XML *generation* only, no untrusted parsing;
* attribute → RBAC-role mapping (spec §"Attribute → role mapping", fail-closed);
* the pure-logic assertion validation policy — rules V3–V9 and V11 over an
  already-parsed, already-signature-verified assertion view;
* the closed, redacted SAML audit record (never NameID / attributes / raw XML).

What is deliberately NOT here (gated on D5, see ``SamlVerifierUnavailableError``):
XML parsing of untrusted assertions, XML-DSIG signature verification (V1/V2)
and the XXE-hardened parser (V10). Hand-rolling XML-DSIG is explicitly
forbidden by ADR-0138 (Alternatives §3). Until a Tier-A library clears the
ADR-0024 full-transitive-tree license gate, the ACS endpoint refuses every
assertion — it never skips signature validation.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The six existing ADR-0018 / ADR-0058 roles. SAML introduces no new role.
ALLOWED_SAML_ROLES: frozenset[str] = frozenset(
    {"reader", "writer", "admin", "auditor", "promoter", "approver"}
)

#: SAML 2.0 success status (spec rule V11).
SAML_STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"

#: Hard cap on clock skew (spec: "default 120 s, hard cap 300 s").
MAX_CLOCK_SKEW_SECONDS = 300

#: ADR-0138 §D5 — no SAML XML-signature library has cleared the ADR-0024
#: transitive-license gate yet, so no signature verifier is available.
SIGNATURE_VERIFIER_AVAILABLE = False

_D5_GATE_MESSAGE = (
    "SAML assertion consumption is not available in this build: the XML "
    "signature-validation library is an open pre-adoption gate (ADR-0138 D5 — "
    "the ADR-0024 full-transitive-tree license audit must pass and a library "
    "version must be pinned before any SAML library ships). NovaFabric refuses "
    "to consume assertions rather than skip signature validation. "
    "Config, SP metadata, role mapping, and validation policy are implemented; "
    "live SSO is pending the D5 gate."
)


# ---------------------------------------------------------------------------
# Named exceptions
# ---------------------------------------------------------------------------


class SamlNotConfiguredError(Exception):
    """SAML backend is absent or disabled in the server config."""


class SamlVerifierUnavailableError(Exception):
    """No XML-signature verifier is available (ADR-0138 D5 open gate)."""

    def __init__(self, message: str = _D5_GATE_MESSAGE) -> None:
        super().__init__(message)


class SamlValidationError(Exception):
    """An assertion failed one of the normative validation rules (V3–V9, V11).

    ``reason`` is a short machine-readable code safe for the redacted audit
    record — it never contains assertion contents.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Config models — Pydantic mirror of server-saml-config.schema.json (closed)
# ---------------------------------------------------------------------------


def _check_roles(roles: list[str]) -> list[str]:
    bad = sorted(set(roles) - ALLOWED_SAML_ROLES)
    if bad:
        allowed = ", ".join(sorted(ALLOWED_SAML_ROLES))
        raise ValueError(f"unknown role(s) {bad!r}; allowed roles: {allowed}")
    return roles


class SamlIdpConfig(BaseModel):
    """Operator-managed IdP descriptor (``server.saml.idp``)."""

    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(min_length=1)
    sso_url: str = Field(min_length=1)
    slo_url: str | None = None
    x509_cert_path: str = Field(min_length=1)
    metadata_path: str | None = None

    @field_validator("sso_url")
    @classmethod
    def _sso_url_is_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("idp.sso_url must be an http(s) URL")
        return v


class SamlAttributeRoleMap(BaseModel):
    """Attribute-value → NovaFabric-role mapping (fail-closed)."""

    model_config = ConfigDict(extra="forbid")

    attribute: str = Field(min_length=1)
    mapping: dict[str, list[str]]
    default_roles: list[str] = Field(default_factory=list)

    @field_validator("mapping")
    @classmethod
    def _mapping_roles_allowed(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        for value, roles in v.items():
            try:
                _check_roles(roles)
            except ValueError as exc:
                raise ValueError(f"mapping[{value!r}]: {exc}") from exc
        return v

    @field_validator("default_roles")
    @classmethod
    def _default_roles_allowed(cls, v: list[str]) -> list[str]:
        return _check_roles(v)


class SamlSloConfig(BaseModel):
    """Single Logout config (``server.saml.slo``). Off by default (D4)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    slo_acs_url: str | None = None


class SamlConfig(BaseModel):
    """The additive ``server.saml`` block (ADR-0029 / ADR-0138).

    Absent block ⇒ SAML backend disabled ⇒ behavior identical to today.
    Unknown keys are rejected (closed schema).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    sp_entity_id: str = Field(min_length=1)
    acs_url: str = Field(min_length=1)
    sp_cert_path: str = Field(min_length=1)
    sp_key_path: str = Field(min_length=1)
    idp: SamlIdpConfig
    attribute_role_map: SamlAttributeRoleMap | None = None
    subject_attribute: str | None = None
    allow_idp_initiated: bool = False
    clock_skew_seconds: int = Field(default=120, ge=0, le=MAX_CLOCK_SKEW_SECONDS)
    slo: SamlSloConfig | None = None
    #: Explicit opt-in to the experimental signxml-backed ACS (ADR-0138). Default
    #: False keeps the safe D5 refusal — the ACS never consumes an assertion. Even
    #: when True, a Security-Architect review remains a pre-production blocking
    #: condition (CLAUDE.md); enabling it acknowledges the pre-review status.
    experimental_acs_enabled: bool = False

    @field_validator("acs_url")
    @classmethod
    def _acs_url_is_http(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("acs_url must be an http(s) URL")
        return v

    @model_validator(mode="after")
    def _slo_requires_idp_slo_url(self) -> "SamlConfig":
        if self.slo is not None and self.slo.enabled and not self.idp.slo_url:
            raise ValueError("slo.enabled requires idp.slo_url")
        return self


# ---------------------------------------------------------------------------
# Parsed assertion view (produced only by a D5-gated verifier)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedAssertion:
    """Signature-verified, parsed view of one SAML assertion.

    Instances are only ever produced by an XML layer that has already enforced
    V1/V2 (signature present + verifies, XSW defense) and V10 (XXE-hardened
    parser). No such layer ships yet (ADR-0138 D5); this type exists so the
    policy layer (V3–V9, V11) is implemented and tested against synthetic data.
    """

    assertion_id: str
    issuer: str
    audiences: tuple[str, ...]
    name_id: str
    not_before: datetime | None
    not_on_or_after: datetime | None
    recipient: str | None
    destination: str | None
    in_response_to: str | None
    status_code: str
    attributes: dict[str, tuple[str, ...]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Replay store (spec rule V8) — bounded, TTL-based, in-memory
# ---------------------------------------------------------------------------


class AssertionReplayStore:
    """Short-lived assertion-ID store for replay rejection (rule V8).

    Bounded: at most ``max_entries`` IDs are retained; expired IDs are purged
    on every call. Single-process only (matches the current server model).
    """

    def __init__(self, ttl_seconds: float = 600.0, max_entries: int = 10_000) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl = ttl_seconds
        self._max = max_entries
        self._seen: dict[str, float] = {}  # assertion ID → expiry (monotonic)

    def _purge(self, now: float) -> None:
        expired = [k for k, exp in self._seen.items() if exp <= now]
        for k in expired:
            del self._seen[k]
        # Bound the store: drop the soonest-to-expire entries if oversized.
        if len(self._seen) > self._max:
            for k, _ in sorted(self._seen.items(), key=lambda kv: kv[1])[
                : len(self._seen) - self._max
            ]:
                del self._seen[k]

    def check_and_record(self, assertion_id: str) -> bool:
        """Record *assertion_id*; return False if it was already seen (replay)."""
        now = time.monotonic()
        self._purge(now)
        if assertion_id in self._seen:
            return False
        self._seen[assertion_id] = now + self._ttl
        return True

    def __len__(self) -> int:
        self._purge(time.monotonic())
        return len(self._seen)


# ---------------------------------------------------------------------------
# Validation policy — normative rules V3–V9 + V11 (V1/V2/V10 are D5-gated)
# ---------------------------------------------------------------------------


def validate_assertion_policy(
    parsed: ParsedAssertion,
    config: SamlConfig,
    *,
    replay_store: AssertionReplayStore,
    pending_request_ids: set[str] | None = None,
    now: datetime | None = None,
) -> None:
    """Apply validation rules V3–V9 and V11 to an already-verified assertion.

    Raises :class:`SamlValidationError` on the first failed rule; returns
    ``None`` when every rule holds. Failure of any check MUST reject the
    assertion and issue no session (spec, normative).

    Precondition: *parsed* came from a signature-verifying, XXE-hardened XML
    layer (V1/V2/V10). That layer does not ship yet (ADR-0138 D5).
    """
    now = now or datetime.now(timezone.utc)
    skew = config.clock_skew_seconds

    # V11 — status Success
    if parsed.status_code != SAML_STATUS_SUCCESS:
        raise SamlValidationError(
            "status_not_success", f"Response status is not Success: {parsed.status_code}"
        )

    # V3 — issuer
    if parsed.issuer != config.idp.entity_id:
        raise SamlValidationError("issuer_mismatch", "assertion issuer is not the configured IdP")

    # V4 — audience
    if config.sp_entity_id not in parsed.audiences:
        raise SamlValidationError(
            "audience_mismatch", "AudienceRestriction does not name this SP"
        )

    # V5 — NotBefore ≤ now + skew
    if parsed.not_before is not None:
        limit = parsed.not_before.timestamp() - skew
        if now.timestamp() < limit:
            raise SamlValidationError("not_yet_valid", "assertion NotBefore is in the future")

    # V6 — NotOnOrAfter > now − skew
    if parsed.not_on_or_after is not None:
        limit = parsed.not_on_or_after.timestamp() + skew
        if now.timestamp() >= limit:
            raise SamlValidationError("expired", "assertion NotOnOrAfter has passed")

    # V7 — Recipient / Destination must match the ACS URL
    for label, value in (("Recipient", parsed.recipient), ("Destination", parsed.destination)):
        if value is not None and value != config.acs_url:
            raise SamlValidationError(
                "recipient_mismatch", f"{label} does not match the configured ACS URL"
            )

    # V9 — InResponseTo: SP-initiated must match a pending AuthnRequest;
    # unsolicited (IdP-initiated) requires the explicit opt-in flag.
    if parsed.in_response_to is None:
        if not config.allow_idp_initiated:
            raise SamlValidationError(
                "idp_initiated_disabled",
                "unsolicited (IdP-initiated) assertion and allow_idp_initiated is false",
            )
    else:
        if parsed.in_response_to not in (pending_request_ids or set()):
            raise SamlValidationError(
                "in_response_to_mismatch",
                "InResponseTo does not match a pending AuthnRequest from this SP",
            )

    # V8 — assertion-ID replay (checked last so rejected assertions
    # do not consume replay-store slots)
    if not replay_store.check_and_record(parsed.assertion_id):
        raise SamlValidationError("replayed", "assertion ID was already consumed")


# ---------------------------------------------------------------------------
# Attribute → role mapping (fail-closed; never escalates)
# ---------------------------------------------------------------------------


def map_attributes_to_roles(
    role_map: SamlAttributeRoleMap,
    attributes: dict[str, tuple[str, ...]] | dict[str, list[str]],
) -> list[str]:
    """Project SAML assertion attributes onto the existing RBAC roles.

    For each value of ``role_map.attribute`` present in *attributes*, union the
    mapped roles. Unmapped values are ignored (fail-closed to no role, never to
    ``admin``). If no value matches, grant ``default_roles`` (default empty).
    Returns a sorted, de-duplicated role list.
    """
    values = attributes.get(role_map.attribute, ())
    granted: set[str] = set()
    for value in values:
        granted.update(role_map.mapping.get(value, ()))
    if not granted:
        granted.update(role_map.default_roles)
    return sorted(granted)


def resolve_subject(parsed: ParsedAssertion, config: SamlConfig) -> str:
    """Return the identity ``sub``: *subject_attribute* if configured, else NameID.

    Ambiguity resolves to rejection (empty subject is an error), never to a
    fallback identity.
    """
    if config.subject_attribute:
        values = parsed.attributes.get(config.subject_attribute, ())
        if len(values) != 1 or not values[0]:
            raise SamlValidationError(
                "subject_unresolved",
                f"subject_attribute {config.subject_attribute!r} is absent or ambiguous",
            )
        return values[0]
    if not parsed.name_id:
        raise SamlValidationError("subject_unresolved", "assertion has no NameID")
    return parsed.name_id


# ---------------------------------------------------------------------------
# Redacted audit record (closed — hygiene rule V-log)
# ---------------------------------------------------------------------------


class SamlAuditEvent(str, enum.Enum):
    accepted = "saml_assertion_accepted"
    rejected = "saml_assertion_rejected"


class SamlAuditRecord(BaseModel):
    """The ONLY thing written about a SAML exchange (spec §V-log).

    Closed model: any extra field (a stray ``name_id``, attribute values, raw
    XML) is structurally rejected. ``sub_hash`` is ``sha256:<64 hex>`` — never
    the subject itself.
    """

    model_config = ConfigDict(extra="forbid")

    event: SamlAuditEvent
    assertion_id: str = Field(min_length=1)
    idp_entity_id: str = Field(min_length=1)
    sub_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    mapped_roles: list[str]
    idp_initiated: bool
    received_at: str
    reject_reason: str | None = None

    @field_validator("mapped_roles")
    @classmethod
    def _roles_allowed(cls, v: list[str]) -> list[str]:
        return _check_roles(v)


def hash_subject(subject: str) -> str:
    """Return the redacted ``sha256:<hex>`` form of a subject identifier."""
    return "sha256:" + hashlib.sha256(subject.encode("utf-8")).hexdigest()


def build_audit_record(
    *,
    accepted: bool,
    assertion_id: str,
    idp_entity_id: str,
    subject: str,
    mapped_roles: list[str],
    idp_initiated: bool,
    reject_reason: str | None = None,
    received_at: datetime | None = None,
) -> SamlAuditRecord:
    """Build the redacted audit record — hashes the subject, keeps no PII."""
    ts = (received_at or datetime.now(timezone.utc)).isoformat()
    return SamlAuditRecord(
        event=SamlAuditEvent.accepted if accepted else SamlAuditEvent.rejected,
        assertion_id=assertion_id,
        idp_entity_id=idp_entity_id,
        sub_hash=hash_subject(subject),
        mapped_roles=mapped_roles,
        idp_initiated=idp_initiated,
        received_at=ts,
        reject_reason=reject_reason,
    )


# ---------------------------------------------------------------------------
# SP metadata emitter (XML generation only — no untrusted parsing)
# ---------------------------------------------------------------------------

_MD_NS = "urn:oasis:names:tc:SAML:2.0:metadata"
_DS_NS = "http://www.w3.org/2000/09/xmldsig#"
_BINDING_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"


def _pem_cert_body(cert_path: Path) -> str | None:
    """Extract the base64 body of a PEM certificate, if readable and well-formed."""
    try:
        text = cert_path.read_text()
    except OSError:
        return None
    lines = [ln.strip() for ln in text.splitlines()]
    body_lines: list[str] = []
    inside = False
    for ln in lines:
        if ln == "-----BEGIN CERTIFICATE-----":
            inside = True
        elif ln == "-----END CERTIFICATE-----":
            break
        elif inside and ln:
            body_lines.append(ln)
    body = "".join(body_lines)
    if not body:
        return None
    try:
        base64.b64decode(body, validate=True)
    except (ValueError, TypeError):
        return None
    return body


def render_sp_metadata(config: SamlConfig) -> str:
    """Render this SP's SAML 2.0 metadata XML for IdP registration.

    Read-only, deterministic, stdlib-only XML *generation* (the D5 gate covers
    parsing/verifying untrusted XML, not emitting our own). Includes the SP
    signing certificate when ``sp_cert_path`` points at a readable PEM cert.
    """
    ET.register_namespace("md", _MD_NS)
    ET.register_namespace("ds", _DS_NS)

    entity = ET.Element(f"{{{_MD_NS}}}EntityDescriptor", {"entityID": config.sp_entity_id})
    spsso = ET.SubElement(
        entity,
        f"{{{_MD_NS}}}SPSSODescriptor",
        {
            "AuthnRequestsSigned": "false",
            "WantAssertionsSigned": "true",
            "protocolSupportEnumeration": "urn:oasis:names:tc:SAML:2.0:protocol",
        },
    )

    cert_body = _pem_cert_body(Path(config.sp_cert_path))
    if cert_body:
        key_desc = ET.SubElement(spsso, f"{{{_MD_NS}}}KeyDescriptor", {"use": "signing"})
        key_info = ET.SubElement(key_desc, f"{{{_DS_NS}}}KeyInfo")
        x509_data = ET.SubElement(key_info, f"{{{_DS_NS}}}X509Data")
        x509_cert = ET.SubElement(x509_data, f"{{{_DS_NS}}}X509Certificate")
        x509_cert.text = cert_body

    if config.slo is not None and config.slo.enabled and config.slo.slo_acs_url:
        ET.SubElement(
            spsso,
            f"{{{_MD_NS}}}SingleLogoutService",
            {"Binding": _BINDING_POST, "Location": config.slo.slo_acs_url},
        )

    ET.SubElement(
        spsso,
        f"{{{_MD_NS}}}AssertionConsumerService",
        {
            "Binding": _BINDING_POST,
            "Location": config.acs_url,
            "index": "0",
            "isDefault": "true",
        },
    )

    xml_body = ET.tostring(entity, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_body + "\n"


# ---------------------------------------------------------------------------
# D5 gate
# ---------------------------------------------------------------------------


def require_signature_verifier(config: "SamlConfig | None" = None) -> None:
    """Refuse assertion consumption unless the experimental ACS is opted into.

    NovaFabric never consumes a SAML assertion without verified XML-DSIG
    signatures (rules V1/V2) and an XXE-hardened parser (V10). The signxml-backed
    verifier that provides them (``server.saml_verify``) cleared the ADR-0024
    license gate in v0.73.0, but consumption stays **off by default**: it is
    enabled only when the operator sets ``server.saml.experimental_acs_enabled``,
    and even then a Security-Architect review is a pre-production blocking
    condition. Without the opt-in (or without the library installed) this raises
    :class:`SamlVerifierUnavailableError`.
    """
    if config is None or not getattr(config, "experimental_acs_enabled", False):
        raise SamlVerifierUnavailableError()
    from novafabric.server import saml_verify

    if not saml_verify.signature_verifier_available():
        raise SamlVerifierUnavailableError(
            "server.saml.experimental_acs_enabled is set but the SAML signature "
            "library is missing; install novafabric[saml] (signxml + lxml)."
        )
