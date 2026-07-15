from __future__ import annotations

import base64
import json
from typing import Any

INTOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"


def make_intoto_statement(
    predicate_type: str,
    subject_name: str,
    subject_sha256: str,
    predicate: dict[str, Any],
) -> dict[str, Any]:
    """Build an in-toto Statement v1.

    `subject_sha256` accepts either 'sha256:<hex>' or '<hex>'.
    """
    digest = subject_sha256.removeprefix("sha256:")
    return {
        "_type": INTOTO_STATEMENT_TYPE,
        "subject": [{"name": subject_name, "digest": {"sha256": digest}}],
        "predicateType": predicate_type,
        "predicate": predicate,
    }


def dsse_pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE Pre-Authentication Encoding.

    PAE(type, body) = "DSSEv1" SP LEN(type) SP type SP LEN(body) SP body
    """
    type_bytes = payload_type.encode()
    return (
        b"DSSEv1 "
        + str(len(type_bytes)).encode()
        + b" "
        + type_bytes
        + b" "
        + str(len(payload)).encode()
        + b" "
        + payload
    )


def dsse_sign_payload(payload: bytes, payload_type: str, signer: Any) -> dict[str, Any]:
    """Wrap arbitrary *payload* bytes in a signed DSSE envelope (the single DSSE writer).

    `signer` must expose `sign(bytes) -> bytes` and `keyid: str`. This is the one place
    DSSE PAE + signing happens; higher-level wrappers (in-toto statements, Evidence-Bundle
    envelopes) all funnel through here so there is never a second DSSE code path.
    """
    pae = dsse_pae(payload_type, payload)
    signature = signer.sign(pae)
    return {
        "payloadType": payload_type,
        "payload": base64.b64encode(payload).decode(),
        "signatures": [
            {
                "keyid": signer.keyid,
                "sig": base64.b64encode(signature).decode(),
            }
        ],
    }


def dsse_sign(statement: dict[str, Any], signer: Any) -> dict[str, Any]:
    """Wrap an in-toto Statement in a signed DSSE envelope.

    `signer` must expose `sign(bytes) -> bytes` and `keyid: str`.
    """
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
    return dsse_sign_payload(payload, DSSE_PAYLOAD_TYPE, signer)


def dsse_verify(envelope: dict[str, Any], verify_fn: Any) -> dict[str, Any]:
    """Verify a DSSE envelope and return the decoded statement.

    `verify_fn(pae_bytes, signature_bytes) -> bool` must perform the actual
    cryptographic check. Raises ValueError on signature failure.
    """
    payload = base64.b64decode(envelope["payload"])
    pae = dsse_pae(envelope["payloadType"], payload)
    sig = base64.b64decode(envelope["signatures"][0]["sig"])
    if not verify_fn(pae, sig):
        raise ValueError("DSSE signature verification failed")
    statement: dict[str, Any] = json.loads(payload)
    return statement
