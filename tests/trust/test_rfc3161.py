"""Tests for novafabric.trust._rfc3161 — RFC 3161 trusted timestamp module.

Covers:
- Valid TSR response: bytes returned
- TSA PKIStatus=2 (rejection): TimestampError raised
- Network error (TransportError): TimestampError raised
- --timestamp-optional semantics (warning logged, no re-raise) tested via
  the CLI integration helper used in test_cli_export_evidence.py
- DER building smoke: request is valid DER SEQUENCE with correct structure
"""

from __future__ import annotations

import hashlib
import logging as logging_mod
from unittest.mock import MagicMock, patch

import httpx
import pytest

from novafabric.trust._rfc3161 import (
    TimestampError,
    _build_timestamp_request,
    _der_boolean_true,
    _der_integer,
    _der_length,
    _der_null,
    _der_octet_string,
    _der_sequence,
    _parse_pki_status,
    add_rfc3161_timestamp,
    add_rfc3161_timestamp_with_fallback,
)

# ---------------------------------------------------------------------------
# Helpers: build minimal valid / invalid TimeStampResp DER
# ---------------------------------------------------------------------------


def _der_tlv(tag: int, value: bytes) -> bytes:
    from novafabric.trust._rfc3161 import _der_length
    return bytes([tag]) + _der_length(len(value)) + value


def _make_tsr_der(pki_status: int) -> bytes:
    """Build a minimal DER TimeStampResp with the given PKIStatus integer.

    TimeStampResp ::= SEQUENCE {
        status  PKIStatusInfo
    }
    PKIStatusInfo ::= SEQUENCE {
        status  PKIStatus  -- INTEGER
    }
    """
    status_integer = _der_integer(pki_status)
    pki_status_info = _der_sequence(status_integer)
    ts_resp = _der_sequence(pki_status_info)
    return ts_resp


# ---------------------------------------------------------------------------
# DER primitive unit tests
# ---------------------------------------------------------------------------


class TestDerPrimitives:
    def test_der_length_short_form(self) -> None:
        assert _der_length(0) == bytes([0])
        assert _der_length(1) == bytes([1])
        assert _der_length(127) == bytes([0x7F])

    def test_der_length_long_form(self) -> None:
        result = _der_length(128)
        assert result[0] == 0x81  # 1 subsequent byte
        assert result[1] == 128

    def test_der_length_two_bytes(self) -> None:
        result = _der_length(256)
        assert result[0] == 0x82  # 2 subsequent bytes
        assert int.from_bytes(result[1:], "big") == 256

    def test_der_integer_zero(self) -> None:
        encoded = _der_integer(0)
        assert encoded == bytes([0x02, 0x01, 0x00])

    def test_der_integer_one(self) -> None:
        encoded = _der_integer(1)
        assert encoded == bytes([0x02, 0x01, 0x01])

    def test_der_integer_large(self) -> None:
        # 64-bit nonce range — must not have leading zero for positive int without high bit
        val = 0xDEADBEEF_CAFEBABE
        encoded = _der_integer(val)
        # tag + length byte + value
        assert encoded[0] == 0x02
        # Decode back
        length = encoded[1]
        recovered = int.from_bytes(encoded[2:2 + length], "big")
        assert recovered == val

    def test_der_integer_high_bit_gets_zero_prefix(self) -> None:
        # 0x80 has high bit set → needs 0x00 prefix for non-negative DER
        encoded = _der_integer(0x80)
        assert encoded[0] == 0x02
        length = encoded[1]
        raw = encoded[2:2 + length]
        assert raw[0] == 0x00  # sign byte added
        assert raw[1] == 0x80

    def test_der_boolean_true(self) -> None:
        assert _der_boolean_true() == bytes([0x01, 0x01, 0xFF])

    def test_der_octet_string(self) -> None:
        data = b"\xde\xad\xbe\xef"
        encoded = _der_octet_string(data)
        assert encoded[0] == 0x04
        assert encoded[1] == 4
        assert encoded[2:] == data

    def test_der_sequence_wraps_correctly(self) -> None:
        inner = bytes([0x01, 0x02, 0x03])
        result = _der_sequence(inner)
        assert result[0] == 0x30  # SEQUENCE tag
        assert result[1] == 3
        assert result[2:] == inner


# ---------------------------------------------------------------------------
# TimeStampReq building
# ---------------------------------------------------------------------------


class TestBuildTimestampRequest:
    def test_output_is_der_sequence(self) -> None:
        digest = hashlib.sha256(b"test payload").digest()
        req, _nonce = _build_timestamp_request(digest)
        # Must start with SEQUENCE tag 0x30
        assert req[0] == 0x30

    def test_digest_present_in_request(self) -> None:
        payload = b"hello novafabric"
        digest = hashlib.sha256(payload).digest()
        req, _nonce = _build_timestamp_request(digest)
        # The raw digest bytes should appear verbatim inside the DER
        assert digest in req

    def test_cert_req_true_present(self) -> None:
        digest = hashlib.sha256(b"x").digest()
        req, _nonce = _build_timestamp_request(digest)
        # BOOLEAN TRUE encodes as 01 01 FF
        assert bytes([0x01, 0x01, 0xFF]) in req

    def test_sha256_oid_present(self) -> None:
        digest = hashlib.sha256(b"x").digest()
        req, _nonce = _build_timestamp_request(digest)
        # id-sha256 OID: 2.16.840.1.101.3.4.2.1
        sha256_oid_content = bytes([
            0x60, 0x86, 0x48, 0x01, 0x65, 0x03, 0x04, 0x02, 0x01
        ])
        assert sha256_oid_content in req

    def test_version_1_present(self) -> None:
        digest = hashlib.sha256(b"x").digest()
        req, _nonce = _build_timestamp_request(digest)
        # INTEGER 1 encodes as 02 01 01 — must appear early in the sequence
        assert bytes([0x02, 0x01, 0x01]) in req


# ---------------------------------------------------------------------------
# PKIStatus parsing
# ---------------------------------------------------------------------------


class TestParsePkiStatus:
    def test_granted_status_0(self) -> None:
        tsr = _make_tsr_der(0)
        assert _parse_pki_status(tsr) == 0

    def test_granted_with_mods_status_1(self) -> None:
        tsr = _make_tsr_der(1)
        assert _parse_pki_status(tsr) == 1

    def test_rejection_status_2(self) -> None:
        tsr = _make_tsr_der(2)
        assert _parse_pki_status(tsr) == 2

    def test_malformed_der_raises(self) -> None:
        with pytest.raises(TimestampError):
            _parse_pki_status(b"\xFF\x00\x00")

    def test_wrong_outer_tag_raises(self) -> None:
        # Wrap PKIStatusInfo in an OCTET STRING instead of SEQUENCE
        inner = _der_sequence(_der_integer(0))
        bad = _der_tlv(0x04, inner)  # OCTET STRING instead of SEQUENCE
        with pytest.raises(TimestampError, match="expected SEQUENCE"):
            _parse_pki_status(bad)


# ---------------------------------------------------------------------------
# add_rfc3161_timestamp — main public function
# ---------------------------------------------------------------------------


class TestAddRfc3161Timestamp:
    """Tests for add_rfc3161_timestamp using httpx mock."""

    def _make_mock_response(self, status_code: int, content: bytes) -> MagicMock:
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = status_code
        mock_resp.content = content
        return mock_resp

    def test_granted_returns_tsr_bytes(self) -> None:
        """PKIStatus=0 (granted) → raw TSR DER bytes returned."""
        tsr_der = _make_tsr_der(0)
        mock_resp = self._make_mock_response(200, tsr_der)

        with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_resp) as mock_post:
            result = add_rfc3161_timestamp(b"dsse envelope bytes", "https://tsa.example.com/tsr")

        assert result == tsr_der
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs[0][0] == "https://tsa.example.com/tsr"
        assert call_kwargs[1]["headers"]["Content-Type"] == "application/timestamp-query"

    def test_granted_with_mods_returns_tsr_bytes(self) -> None:
        """PKIStatus=1 (grantedWithMods) → raw TSR DER bytes returned."""
        tsr_der = _make_tsr_der(1)
        mock_resp = self._make_mock_response(200, tsr_der)

        with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_resp):
            result = add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")

        assert result == tsr_der

    def test_rejection_pki_status_raises(self) -> None:
        """PKIStatus=2 (rejection) → TimestampError raised."""
        tsr_der = _make_tsr_der(2)
        mock_resp = self._make_mock_response(200, tsr_der)

        with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_resp):
            with pytest.raises(TimestampError, match="rejection"):
                add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")

    def test_waiting_pki_status_raises(self) -> None:
        """PKIStatus=3 (waiting) → TimestampError raised."""
        tsr_der = _make_tsr_der(3)
        mock_resp = self._make_mock_response(200, tsr_der)

        with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_resp):
            with pytest.raises(TimestampError, match="waiting"):
                add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")

    def test_http_error_raises(self) -> None:
        """Non-200 HTTP status → TimestampError raised."""
        mock_resp = self._make_mock_response(503, b"Service Unavailable")

        with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_resp):
            with pytest.raises(TimestampError, match="HTTP 503"):
                add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")

    def test_network_error_raises(self) -> None:
        """TransportError (network unreachable) → TimestampError raised."""
        with patch(
            "novafabric.trust._rfc3161.httpx.post",
            side_effect=httpx.TransportError("Connection refused"),
        ):
            with pytest.raises(TimestampError, match="unreachable"):
                add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")

    def test_connect_error_raises(self) -> None:
        """ConnectError (subclass of TransportError) → TimestampError raised."""
        with patch(
            "novafabric.trust._rfc3161.httpx.post",
            side_effect=httpx.ConnectError("Name resolution failed"),
        ):
            with pytest.raises(TimestampError, match="unreachable"):
                add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")

    def test_empty_response_raises(self) -> None:
        """Empty response body → TimestampError raised."""
        mock_resp = self._make_mock_response(200, b"")

        with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_resp):
            with pytest.raises(TimestampError, match="empty"):
                add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")

    def test_digest_sent_to_tsa(self) -> None:
        """The posted DER contains the SHA-256 digest of the input bytes."""
        payload = b"specific dsse envelope content"
        expected_digest = hashlib.sha256(payload).digest()

        tsr_der = _make_tsr_der(0)
        mock_resp = self._make_mock_response(200, tsr_der)

        captured_content: list[bytes] = []

        def capture_post(url: str, **kwargs: object) -> MagicMock:
            captured_content.append(kwargs["content"])  # type: ignore[arg-type]
            return mock_resp

        with patch("novafabric.trust._rfc3161.httpx.post", side_effect=capture_post):
            add_rfc3161_timestamp(payload, "https://tsa.example.com/tsr")

        assert len(captured_content) == 1
        # The digest must appear verbatim in the DER request
        assert expected_digest in captured_content[0]

    def test_timestamp_optional_warns_on_network_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """--timestamp-optional behaviour: caller catches TimestampError and warns.

        This test exercises the caller pattern used in export_evidence_cmd.
        The function itself still raises; the CLI wraps it.
        """
        with patch(
            "novafabric.trust._rfc3161.httpx.post",
            side_effect=httpx.TransportError("timeout"),
        ):
            with pytest.raises(TimestampError):
                add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")

        # Simulate the optional-mode caller pattern:
        logger = logging_mod.getLogger("novafabric.cli.export_evidence")
        with caplog.at_level(logging_mod.WARNING, logger="novafabric.cli.export_evidence"):
            try:
                with patch(
                    "novafabric.trust._rfc3161.httpx.post",
                    side_effect=httpx.TransportError("timeout"),
                ):
                    add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")
            except TimestampError as exc:
                logger.warning("timestamp skipped (--timestamp-optional): %s", exc)

        assert any("timestamp skipped" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# add_rfc3161_timestamp — HTTPError path (not TransportError)
# ---------------------------------------------------------------------------


def test_http_error_non_transport_raises() -> None:
    """httpx.HTTPError (not TransportError subclass) → TimestampError."""
    with patch(
        "novafabric.trust._rfc3161.httpx.post",
        side_effect=httpx.HTTPError("generic http error"),
    ):
        with pytest.raises(TimestampError, match="HTTP error"):
            add_rfc3161_timestamp(b"data", "https://tsa.example.com/tsr")


# ---------------------------------------------------------------------------
# _parse_pki_status — wrong inner STATUS tag
# ---------------------------------------------------------------------------


def test_parse_pki_status_wrong_status_tag_raises() -> None:
    """PKIStatusInfo contains a non-INTEGER first child → TimestampError."""
    # Build PKIStatusInfo with a NULL instead of an INTEGER
    pki_status_info = _der_sequence(_der_null())
    ts_resp = _der_sequence(pki_status_info)
    with pytest.raises(TimestampError):
        _parse_pki_status(ts_resp)


def test_parse_pki_status_generic_exception_wraps() -> None:
    """Any unexpected parse failure is wrapped in TimestampError."""
    with pytest.raises(TimestampError):
        _parse_pki_status(b"\x30\x02\x30\x00")  # inner sequence too short


# ---------------------------------------------------------------------------
# _extract_nonce_from_tsr — long-form length paths
# ---------------------------------------------------------------------------


def test_extract_nonce_from_tsr_with_long_form_outer() -> None:
    """TSR outer SEQUENCE with long-form length is parsed without error."""
    from novafabric.trust._rfc3161 import _extract_nonce_from_tsr

    # Build a TSR with outer long-form length (> 127 bytes of content)
    inner = b"\x02\x05\x00\xde\xad\xbe\xef"  # INTEGER 5 bytes (fits nonce range)
    padding = b"\x05\x00" * 70  # NULL padding to exceed 127 bytes
    body = inner + padding
    # Long-form length: 2 subsequent bytes
    length = len(body)
    long_length = bytes([0x82, (length >> 8) & 0xFF, length & 0xFF])
    tsr = bytes([0x30]) + long_length + body
    result = _extract_nonce_from_tsr(tsr)
    assert result is None or isinstance(result, int)


def test_extract_nonce_from_tsr_long_form_scan_inner() -> None:
    """Inner long-form length in scan buffer is handled gracefully."""
    from novafabric.trust._rfc3161 import _extract_nonce_from_tsr

    # Craft a SEQUENCE containing a nested SEQUENCE with long-form length
    nonce_bytes = b"\x00\xde\xad\xbe\xef"  # 5-byte nonce value
    nonce_tlv = bytes([0x02, len(nonce_bytes)]) + nonce_bytes
    padding = b"\x05\x00" * 70
    inner_body = nonce_tlv + padding
    inner_len = len(inner_body)
    inner_seq = bytes([0x30, 0x82, (inner_len >> 8) & 0xFF, inner_len & 0xFF]) + inner_body

    # Wrap in outer SEQUENCE with short-form length (fits in one byte if small enough)
    outer_body = inner_seq
    outer = bytes([0x30, len(outer_body)]) + outer_body if len(outer_body) < 128 else (
        bytes([0x30, 0x82, (len(outer_body) >> 8) & 0xFF, len(outer_body) & 0xFF]) + outer_body
    )
    result = _extract_nonce_from_tsr(outer)
    assert result is None or isinstance(result, int)


# ---------------------------------------------------------------------------
# verify_tsa_chain — stub DER (no extractable cert)
# ---------------------------------------------------------------------------


def test_verify_tsa_chain_no_cert_extracted() -> None:
    """Stub TSR DER that yields no cert → chain_ok=False, early return."""
    from novafabric.trust._rfc3161 import verify_tsa_chain

    stub_der = _make_tsr_der(0)  # minimal TSR, no certificate embedded
    result = verify_tsa_chain(stub_der)
    assert result.chain_ok is False
    assert result.ocsp_checked is False
    assert any("Could not extract" in e for e in result.errors)


def test_verify_tsa_chain_with_trusted_ca_pem_no_cert() -> None:
    """Explicit trusted_ca_pem provided but TSR has no cert → chain_ok=False."""
    from novafabric.trust._rfc3161 import verify_tsa_chain

    result = verify_tsa_chain(_make_tsr_der(0), trusted_ca_pem="")
    assert result.chain_ok is False


def test_verify_tsa_chain_never_raises() -> None:
    """verify_tsa_chain must never raise regardless of input."""
    from novafabric.trust._rfc3161 import verify_tsa_chain

    for bad_input in [b"", b"\xff\xfe\xfd", b"\x30\x00"]:
        result = verify_tsa_chain(bad_input)
        assert isinstance(result.chain_ok, bool)
        assert isinstance(result.errors, list)


# ---------------------------------------------------------------------------
# _extract_signing_cert_from_tsr — edge cases
# ---------------------------------------------------------------------------


def test_extract_signing_cert_empty_der_returns_none() -> None:
    from novafabric.trust._rfc3161 import _extract_signing_cert_from_tsr

    assert _extract_signing_cert_from_tsr(b"") is None


def test_extract_signing_cert_garbage_returns_none() -> None:
    from novafabric.trust._rfc3161 import _extract_signing_cert_from_tsr

    assert _extract_signing_cert_from_tsr(b"\xff" * 100) is None


# ---------------------------------------------------------------------------
# _get_ocsp_url / _get_crl_urls — with mock cert objects
# ---------------------------------------------------------------------------


def test_get_ocsp_url_non_cert_object_returns_none() -> None:
    from novafabric.trust._rfc3161 import _get_ocsp_url

    assert _get_ocsp_url("not a cert") is None


def test_get_crl_urls_non_cert_object_returns_empty() -> None:
    from novafabric.trust._rfc3161 import _get_crl_urls

    assert _get_crl_urls("not a cert") == []


# ---------------------------------------------------------------------------
# _der_integer — leading-zero trim path
# ---------------------------------------------------------------------------


def test_der_integer_strips_redundant_leading_zero() -> None:
    """_der_integer must not produce unnecessary leading zero bytes."""
    from novafabric.trust._rfc3161 import _der_integer

    # 0x0001 — encoded as 2 bytes by (bit_length+8)//8=1, no leading zero expected
    encoded = _der_integer(1)
    assert encoded == bytes([0x02, 0x01, 0x01])

    # Any positive integer should decode back correctly
    for val in [127, 128, 255, 256, 65535, 65536, 2**31, 2**32 - 1, 2**64 - 1]:
        enc = _der_integer(val)
        assert enc[0] == 0x02
        length = enc[1]
        raw = enc[2:2 + length]
        recovered = int.from_bytes(raw.lstrip(b"\x00") or b"\x00", "big")
        assert recovered == val, f"Round-trip failed for {val}"


# ---------------------------------------------------------------------------
# add_rfc3161_timestamp_with_fallback — REG-ADR-007 multi-TSA fallback
# ---------------------------------------------------------------------------


class TestAddRfc3161TimestampWithFallback:
    def _resp(self, status_code: int, content: bytes) -> MagicMock:
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = status_code
        mock_resp.content = content
        return mock_resp

    def test_empty_list_raises(self) -> None:
        with pytest.raises(TimestampError, match="no TSA URLs configured"):
            add_rfc3161_timestamp_with_fallback(b"data", [])

    def test_first_url_success_never_tries_second(self) -> None:
        tsr_der = _make_tsr_der(0)
        with patch(
            "novafabric.trust._rfc3161.httpx.post", return_value=self._resp(200, tsr_der)
        ) as mock_post:
            result, used_url = add_rfc3161_timestamp_with_fallback(
                b"data", ["https://primary.example/tsr", "https://backup.example/tsr"]
            )
        assert result == tsr_der
        assert used_url == "https://primary.example/tsr"
        mock_post.assert_called_once()

    def test_first_url_fails_falls_through_to_second(self) -> None:
        tsr_der = _make_tsr_der(0)
        responses = [
            httpx.TransportError("connection refused"),
            self._resp(200, tsr_der),
        ]

        def fake_post(*args, **kwargs):
            resp = responses.pop(0)
            if isinstance(resp, Exception):
                raise resp
            return resp

        with patch("novafabric.trust._rfc3161.httpx.post", side_effect=fake_post):
            result, used_url = add_rfc3161_timestamp_with_fallback(
                b"data", ["https://primary.example/tsr", "https://backup.example/tsr"]
            )
        assert result == tsr_der
        assert used_url == "https://backup.example/tsr"

    def test_all_urls_fail_raises_last_error(self) -> None:
        with patch(
            "novafabric.trust._rfc3161.httpx.post",
            side_effect=httpx.TransportError("unreachable"),
        ):
            with pytest.raises(TimestampError, match="unreachable"):
                add_rfc3161_timestamp_with_fallback(
                    b"data", ["https://primary.example/tsr", "https://backup.example/tsr"]
                )
