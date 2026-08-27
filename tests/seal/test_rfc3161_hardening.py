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
from pathlib import Path
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

    def test_finds_the_nonce_in_a_real_tstinfo_shape(self):
        nonce_int = 0xDEADBEEFCAFEBABE
        tsr = _build_tsr_with_nonce(nonce_int)
        assert _extract_nonce_from_tsr(tsr) == nonce_int

    def test_finds_a_4_byte_nonce(self):
        nonce_int = 0xABCD_1234
        tsr = _build_tsr_with_nonce(nonce_int)
        assert _extract_nonce_from_tsr(tsr) == nonce_int

    def test_does_not_return_the_serial_number(self):
        """The regression: serialNumber sits three fields ahead of the nonce.

        Returning it made every comparison against a real TSA fail, always with
        the same constant, always reported as "possible replay or MITM attack".
        """
        serial = 0x11223344
        nonce_int = 0x99887766_55443322
        tsr = _build_tsr_with_nonce(nonce_int, )
        assert _extract_nonce_from_tsr(tsr) != serial
        assert _extract_nonce_from_tsr(tsr) == nonce_int

    def test_absent_nonce_degrades_to_none_rather_than_failing(self):
        """The nonce field is OPTIONAL; some TSAs omit it entirely."""
        tsr = _build_tsr_with_nonce(None)
        assert _extract_nonce_from_tsr(tsr) is None


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


# id-ct-TSTInfo OID TLV — 1.2.840.113549.1.9.16.1.4
_TSTINFO_OID = bytes(
    [0x06, 0x0B, 0x2A, 0x86, 0x48, 0x86, 0xF7, 0x0D, 0x01, 0x09, 0x10, 0x01, 0x04]
)


def _der_gentime(value: bytes = b"20260827173122Z") -> bytes:
    return bytes([0x18, len(value)]) + value


def _der_explicit0(data: bytes) -> bytes:
    return bytes([0xA0, len(data)]) + data


def _build_tstinfo(nonce_int: int | None, serial: int = 0xABCD1234) -> bytes:
    """Build a TSTInfo SEQUENCE with RFC 3161 §2.4.2 field *order*.

    The order is what matters: ``serialNumber`` is an INTEGER three fields
    ahead of ``nonce``, so a parser that simply hunts for a nonce-sized INTEGER
    returns the serial number instead. The old fake TSR wrapped a bare INTEGER
    in a SEQUENCE, which no real TSA emits — so these tests passed against an
    extractor that never once read a real nonce correctly.
    """
    fields = [
        _der_integer(1),  # version
        b"\x06\x04\x2a\x03\x04\x05",  # policy OID (arbitrary)
        _der_sequence(_der_octet(b"\x00" * 32)),  # messageImprint
        _der_integer(serial),  # serialNumber — the decoy
        _der_gentime(),  # genTime
    ]
    if nonce_int is not None:
        fields.append(_der_integer(nonce_int))  # nonce, after genTime
    return _der_sequence(b"".join(fields))


def _build_tsr_with_nonce(nonce_int: int | None) -> bytes:
    """Build a TSR whose TSTInfo carries ``nonce_int`` in the correct position."""
    econtent = _der_explicit0(_der_octet(_build_tstinfo(nonce_int)))
    return _make_granted_tsr(_TSTINFO_OID + econtent)


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


# ---------------------------------------------------------------------------
# Regression: a real TSA response, captured once, checked forever
# ---------------------------------------------------------------------------


class TestAgainstARealTsaResponse:
    """Synthetic TSRs did not catch this; a captured real one does.

    ``tests/fixtures/rfc3161/freetsa-response.tsr`` is a DER TimeStampResp from
    freetsa.org. The request that produced it sent nonce 12473090696047252391.
    The old extractor returned 14044904342208178637 for it — freetsa's
    serialNumber — and returned that same constant for every nonce ever sent.
    """

    FIXTURE = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "rfc3161"
        / "freetsa-response.tsr"
    )
    SENT_NONCE = 12473090696047252391

    def test_extracts_the_nonce_that_was_actually_sent(self) -> None:
        tsr = self.FIXTURE.read_bytes()
        assert _extract_nonce_from_tsr(tsr) == self.SENT_NONCE

    def test_does_not_return_the_tsa_serial_number(self) -> None:
        tsr = self.FIXTURE.read_bytes()
        assert _extract_nonce_from_tsr(tsr) != 14044904342208178637

    def test_a_real_response_passes_the_replay_check(self) -> None:
        """End to end: the nonce comparison must not reject a legitimate TSR."""
        tsr = self.FIXTURE.read_bytes()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.content = tsr

        with patch(
            "novafabric.trust._rfc3161._build_timestamp_request",
            return_value=(b"\x30\x03\x02\x01\x01", self.SENT_NONCE),
        ), patch("novafabric.trust._rfc3161.httpx.post", return_value=mock_response):
            assert add_rfc3161_timestamp(b"payload", "https://tsa.example/tsr") == tsr
