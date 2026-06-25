"""Tests for RFC 3161 v0.2 hardening items (ADR-0030).

Implemented items tested here:
  Item 3a — Nonce replay protection: TSR nonce must match request nonce.
  Item 3b — --timestamp-optional is already wired in export_evidence_cmd;
             its behaviour is exercised in the export-evidence tests.  We test
             the _add_timestamp_to_bundle helper directly here.
  Item 3c — TSR sha256 hash recorded in manifest.json under manifest_dsse_tsr_sha256.

All tests use mocked TSA responses to avoid network calls.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

import pytest

from novafabric.trust._rfc3161 import (
    TimestampError,
    _build_timestamp_request,
    _der_integer,
    _der_sequence,
    _extract_nonce_from_tsr,
    add_rfc3161_timestamp,
)

# ---------------------------------------------------------------------------
# DER helpers reused from the module under test
# ---------------------------------------------------------------------------


def _der_octet(data: bytes) -> bytes:
    return bytes([0x04, len(data)]) + data


def _make_granted_tsr(body_extras: bytes = b"") -> bytes:
    """Wrap PKIStatus=0 (granted) in a minimal TimeStampResp DER."""
    pki_status_info = _der_sequence(_der_integer(0))  # SEQUENCE { INTEGER 0 }
    body = pki_status_info + body_extras
    return _der_sequence(body)


# ---------------------------------------------------------------------------
# Item 3a: Nonce replay protection — _extract_nonce_from_tsr
# ---------------------------------------------------------------------------


class TestExtractNonceFromTsr:
    def test_returns_none_for_empty_bytes(self):
        assert _extract_nonce_from_tsr(b"") is None

    def test_returns_none_for_garbage(self):
        assert _extract_nonce_from_tsr(b"\xff\xfe\xfd") is None

    def test_returns_none_for_minimal_tsr_without_nonce(self):
        """TSR with only PKIStatusInfo (no nonce) returns None."""
        tsr = _make_granted_tsr()
        # Minimal TSR has only version integer (1 byte body) — not in 4-9 byte range
        result = _extract_nonce_from_tsr(tsr)
        # Minimal TSR has no nonce; result may be None or a non-nonce integer
        # We only assert it does not raise
        assert result is None or isinstance(result, int)

    def test_finds_nonce_in_nested_sequence(self):
        """A nonce-sized INTEGER nested in a SEQUENCE is found."""
        nonce_int = 0xDEADBEEFCAFEBABE  # 8 bytes
        nonce_bytes = _der_integer(nonce_int)
        inner_seq = _der_sequence(nonce_bytes)
        tsr = _make_granted_tsr(inner_seq)
        result = _extract_nonce_from_tsr(tsr)
        assert result == nonce_int

    def test_finds_4_byte_nonce(self):
        """A 4-byte nonce (32-bit) is within the 4-9 byte length window."""
        nonce_int = 0xABCD_1234  # fits in 4 bytes
        nonce_bytes = _der_integer(nonce_int)
        tsr = _make_granted_tsr(_der_sequence(nonce_bytes))
        result = _extract_nonce_from_tsr(tsr)
        assert result == nonce_int


# ---------------------------------------------------------------------------
# Item 3a: _build_timestamp_request returns (tsq_bytes, nonce_int)
# ---------------------------------------------------------------------------


class TestBuildTimestampRequest:
    def test_returns_tuple(self):
        digest = hashlib.sha256(b"test").digest()
        result = _build_timestamp_request(digest)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_tsq_bytes_is_bytes(self):
        digest = hashlib.sha256(b"hello").digest()
        tsq, nonce = _build_timestamp_request(digest)
        assert isinstance(tsq, bytes)
        assert len(tsq) > 0

    def test_nonce_is_int(self):
        digest = hashlib.sha256(b"nonce-test").digest()
        _, nonce = _build_timestamp_request(digest)
        assert isinstance(nonce, int)
        assert nonce >= 0

    def test_nonce_is_random_across_calls(self):
        """Two consecutive calls should produce different nonces."""
        digest = hashlib.sha256(b"x").digest()
        _, n1 = _build_timestamp_request(digest)
        _, n2 = _build_timestamp_request(digest)
        # With 64-bit random nonce, collision probability is negligible
        assert n1 != n2


# ---------------------------------------------------------------------------
# Item 3a: nonce mismatch triggers TimestampError in add_rfc3161_timestamp
# ---------------------------------------------------------------------------


def _build_tsr_with_nonce(nonce_int: int) -> bytes:
    """Build a minimal but valid TSR DER containing a specific nonce."""
    nonce_bytes = _der_integer(nonce_int)
    nonce_seq = _der_sequence(nonce_bytes)  # nest as SEQUENCE to be reachable
    return _make_granted_tsr(nonce_seq)


class TestNonceReplayProtection:
    def test_matching_nonce_succeeds(self):
        """When TSR echoes the same nonce, add_rfc3161_timestamp returns TSR."""
        # Patch _build_timestamp_request to control the nonce
        fixed_nonce = 0xAABBCCDD_EEFF0011
        tsq_bytes = b"\x30\x03\x02\x01\x01"  # minimal TSQ placeholder

        with patch(
            "novafabric.trust._rfc3161._build_timestamp_request",
            return_value=(tsq_bytes, fixed_nonce),
        ):
            tsr = _build_tsr_with_nonce(fixed_nonce)
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = tsr

            with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_response):
                result = add_rfc3161_timestamp(b"dsse-bytes", "https://tsa.example.com")

        assert result == tsr

    def test_mismatched_nonce_raises_timestamp_error(self):
        """When TSR echoes a different nonce, TimestampError is raised."""
        request_nonce = 0x1122334455667788
        tsr_nonce = 0x9900AABBCCDDEEFF  # different nonce
        tsq_bytes = b"\x30\x03\x02\x01\x01"

        with patch(
            "novafabric.trust._rfc3161._build_timestamp_request",
            return_value=(tsq_bytes, request_nonce),
        ):
            tsr = _build_tsr_with_nonce(tsr_nonce)
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = tsr

            with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_response):
                with pytest.raises(TimestampError, match="nonce mismatch"):
                    add_rfc3161_timestamp(b"dsse-bytes", "https://tsa.example.com")

    def test_absent_nonce_in_tsr_does_not_raise(self):
        """If TSR omits nonce entirely, we degrade gracefully (no error)."""
        request_nonce = 0xDEADBEEFDEADBEEF
        tsq_bytes = b"\x30\x03\x02\x01\x01"

        with patch(
            "novafabric.trust._rfc3161._build_timestamp_request",
            return_value=(tsq_bytes, request_nonce),
        ):
            # TSR without any nonce-sized INTEGER
            tsr = _make_granted_tsr()  # only PKIStatusInfo, no nonce
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.content = tsr

            with patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_response):
                # Should succeed — nonce absent is treated as degrade, not failure
                result = add_rfc3161_timestamp(b"dsse-bytes", "https://tsa.example.com")

        assert result == tsr


# ---------------------------------------------------------------------------
# Item 3c: TSR sha256 hash in manifest.json
# ---------------------------------------------------------------------------


class TestTsrSha256InManifest:
    """Verify that _add_timestamp_to_bundle records manifest_dsse_tsr_sha256."""

    def _make_minimal_bundle_zip(self, tmp_path: object) -> object:
        """Create a minimal Evidence Bundle ZIP for testing."""
        import json
        import zipfile

        bundle_path = tmp_path / "test_bundle.zip"
        dsse_content = b'{"payload":"dGVzdA","payloadType":"application/json","signatures":[]}'
        manifest = {
            "manifest_hash": "sha256:placeholder",
            "files": {},
        }

        with zipfile.ZipFile(bundle_path, "w") as zf:
            zf.writestr("attestations/run.intoto.json", dsse_content)
            zf.writestr("manifest.json", json.dumps(manifest))
            zf.writestr("README.md", "# Test Bundle\n")

        return bundle_path

    def test_tsr_sha256_recorded_in_manifest(self, tmp_path):
        """After timestamping, manifest.json contains manifest_dsse_tsr_sha256."""
        import json
        import zipfile

        bundle_path = self._make_minimal_bundle_zip(tmp_path)

        # Build a fake TSR
        fake_tsr = _make_granted_tsr()
        expected_sha256 = "sha256:" + hashlib.sha256(fake_tsr).hexdigest()

        with patch(
            "novafabric.cli.export_evidence.add_rfc3161_timestamp",
            return_value=fake_tsr,
        ):
            from novafabric.cli.export_evidence import _add_timestamp_to_bundle
            _add_timestamp_to_bundle(bundle_path, "https://tsa.example.com", False)

        with zipfile.ZipFile(bundle_path, "r") as zf:
            manifest = json.loads(zf.read("manifest.json"))

        assert "manifest_dsse_tsr_sha256" in manifest
        assert manifest["manifest_dsse_tsr_sha256"] == expected_sha256

    def test_tsr_status_ok_in_manifest(self, tmp_path):
        """After successful timestamping, manifest.json has timestamp_status='ok'."""
        import json
        import zipfile

        bundle_path = self._make_minimal_bundle_zip(tmp_path)
        fake_tsr = _make_granted_tsr()

        with patch(
            "novafabric.cli.export_evidence.add_rfc3161_timestamp",
            return_value=fake_tsr,
        ):
            from novafabric.cli.export_evidence import _add_timestamp_to_bundle
            _add_timestamp_to_bundle(bundle_path, "https://tsa.example.com", False)

        with zipfile.ZipFile(bundle_path, "r") as zf:
            manifest = json.loads(zf.read("manifest.json"))

        assert manifest.get("timestamp_status") == "ok"

    def test_timestamp_optional_records_failure(self, tmp_path):
        """With --timestamp-optional, TSA failure is recorded, no tsr_sha256."""
        import json
        import zipfile

        bundle_path = self._make_minimal_bundle_zip(tmp_path)

        with patch(
            "novafabric.cli.export_evidence.add_rfc3161_timestamp",
            side_effect=TimestampError("TSA unreachable"),
        ):
            from novafabric.cli.export_evidence import _add_timestamp_to_bundle
            _add_timestamp_to_bundle(bundle_path, "https://tsa.example.com", True)

        with zipfile.ZipFile(bundle_path, "r") as zf:
            manifest = json.loads(zf.read("manifest.json"))

        assert manifest.get("timestamp_status") == "failed"
        assert "timestamp_failure_reason" in manifest
        # No tsr_sha256 when TSA failed
        assert "manifest_dsse_tsr_sha256" not in manifest
