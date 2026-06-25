"""Tests for SigstoreSigner and SigstoreBundleStore (ADR-0071).

ALL Sigstore SDK / network calls are mocked — no real OIDC or Rekor calls.

Covers:
1. SigstoreSigner.sign_artifact raises ImportError with clear message when sigstore not installed
2. SigstoreBundleStore.store_bundle writes JSON to correct path
3. SigstoreBundleStore.load_bundle returns None for non-existent capsule
4. SigstoreBundleStore.load_bundle returns stored bundle dict
5. nova seal sign --backend sigstore with sigstore not installed exits 1 with clear message
6. nova seal sign --help includes --backend
7. SigstoreVerificationResult with valid=False reports error correctly
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.trust.novaseal.sigstore_signer import (
    SigstoreBundleStore,
    SigstoreSigner,
    SigstoreVerificationResult,
)

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_BUNDLE: dict = {
    "mediaType": "application/vnd.dev.sigstore.bundle+json;version=0.3",
    "verificationMaterial": {
        "tlogEntries": [
            {
                "logIndex": "42",
                "logId": {"keyId": "abc123"},
                "kindVersion": {"kind": "hashedrekord", "version": "0.0.1"},
                "integratedTime": "1716825600",
                "inclusionPromise": {"signedEntryTimestamp": "BASE64=="},
                "canonicalizedBody": "BASE64==",
            }
        ],
        "x509CertificateChain": {"certificates": []},
    },
    "dsseEnvelope": {
        "payload": "BASE64==",
        "payloadType": "application/vnd.dev.sigstore.bundle+json",
        "signatures": [],
    },
}


# ---------------------------------------------------------------------------
# Test 1: SigstoreSigner.sign_artifact raises ImportError when sigstore absent
# ---------------------------------------------------------------------------

class TestSigstoreSignerImportError:
    def test_sign_artifact_raises_import_error_when_sigstore_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sign_artifact() raises ImportError with install hint when sigstore absent."""
        monkeypatch.setitem(sys.modules, "sigstore", None)  # type: ignore[arg-type]
        monkeypatch.setitem(sys.modules, "sigstore.sign", None)  # type: ignore[arg-type]

        signer = SigstoreSigner()
        with pytest.raises(ImportError, match="pip install novafabric\\[sigstore\\]"):
            signer.sign_artifact(b'{"capsule_id": "test"}')

    def test_sign_artifact_raises_import_error_message_is_actionable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ImportError message must mention novafabric[sigstore]."""
        monkeypatch.setitem(sys.modules, "sigstore", None)  # type: ignore[arg-type]
        monkeypatch.setitem(sys.modules, "sigstore.sign", None)  # type: ignore[arg-type]

        signer = SigstoreSigner()
        with pytest.raises(ImportError) as exc_info:
            signer.sign_artifact(b"hello")
        assert "novafabric[sigstore]" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Test 2: SigstoreBundleStore.store_bundle writes JSON to correct path
# ---------------------------------------------------------------------------

class TestSigstoreBundleStoreWrite:
    def test_store_bundle_writes_json_to_correct_path(self, tmp_path: Path) -> None:
        """store_bundle() writes bundle JSON to <home>/sigstore/<capsule_id>.bundle.json."""
        capsule_id = "abc123deadbeef"
        bundle = {"foo": "bar", "nested": {"key": "value"}}

        result_path = SigstoreBundleStore.store_bundle(capsule_id, bundle, home=tmp_path)

        expected_path = tmp_path / "sigstore" / f"{capsule_id}.bundle.json"
        assert result_path == expected_path
        assert expected_path.exists()
        written = json.loads(expected_path.read_text())
        assert written == bundle

    def test_store_bundle_creates_parent_dirs(self, tmp_path: Path) -> None:
        """store_bundle() creates the sigstore/ sub-directory if absent."""
        capsule_id = "testcapsule"
        bundle_dir = tmp_path / "sigstore"
        assert not bundle_dir.exists()

        SigstoreBundleStore.store_bundle(capsule_id, {"x": 1}, home=tmp_path)

        assert bundle_dir.exists()
        assert bundle_dir.is_dir()

    def test_store_bundle_with_sample_bundle_content(self, tmp_path: Path) -> None:
        """store_bundle() correctly serialises the full sample bundle."""
        capsule_id = "sha256-hexdigest"
        result_path = SigstoreBundleStore.store_bundle(capsule_id, _SAMPLE_BUNDLE, tmp_path)

        loaded = json.loads(result_path.read_text())
        assert loaded["mediaType"] == _SAMPLE_BUNDLE["mediaType"]
        assert loaded["verificationMaterial"]["tlogEntries"][0]["logIndex"] == "42"


# ---------------------------------------------------------------------------
# Test 3: SigstoreBundleStore.load_bundle returns None for non-existent capsule
# ---------------------------------------------------------------------------

class TestSigstoreBundleStoreLoadMissing:
    def test_load_bundle_returns_none_when_absent(self, tmp_path: Path) -> None:
        """load_bundle() returns None when no bundle file exists for capsule_id."""
        result = SigstoreBundleStore.load_bundle("nonexistent-id", home=tmp_path)
        assert result is None

    def test_load_bundle_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        """load_bundle() returns None when the sigstore/ directory doesn't exist."""
        fresh_dir = tmp_path / "fresh"
        fresh_dir.mkdir()
        result = SigstoreBundleStore.load_bundle("any-id", home=fresh_dir)
        assert result is None


# ---------------------------------------------------------------------------
# Test 4: SigstoreBundleStore.load_bundle returns stored bundle dict
# ---------------------------------------------------------------------------

class TestSigstoreBundleStoreLoadPresent:
    def test_load_bundle_returns_stored_dict(self, tmp_path: Path) -> None:
        """load_bundle() returns the exact bundle dict that was stored."""
        capsule_id = "round-trip-test"
        original = {"payload": "hello", "sig": "world", "meta": {"v": 3}}

        SigstoreBundleStore.store_bundle(capsule_id, original, home=tmp_path)
        loaded = SigstoreBundleStore.load_bundle(capsule_id, home=tmp_path)

        assert loaded == original

    def test_load_bundle_returns_sample_bundle(self, tmp_path: Path) -> None:
        """load_bundle() correctly round-trips the sample bundle fixture."""
        capsule_id = "sample-bundle-capsule"

        SigstoreBundleStore.store_bundle(capsule_id, _SAMPLE_BUNDLE, home=tmp_path)
        loaded = SigstoreBundleStore.load_bundle(capsule_id, home=tmp_path)

        assert loaded is not None
        assert loaded["mediaType"] == _SAMPLE_BUNDLE["mediaType"]
        tlog = loaded["verificationMaterial"]["tlogEntries"][0]
        assert tlog["logIndex"] == "42"

    def test_load_bundle_handles_invalid_json_gracefully(self, tmp_path: Path) -> None:
        """load_bundle() returns None on corrupt JSON rather than raising."""
        capsule_id = "corrupt-capsule"
        bundle_dir = tmp_path / "sigstore"
        bundle_dir.mkdir(parents=True)
        corrupt_file = bundle_dir / f"{capsule_id}.bundle.json"
        corrupt_file.write_text("NOT VALID JSON {{{{", encoding="utf-8")

        result = SigstoreBundleStore.load_bundle(capsule_id, home=tmp_path)
        assert result is None


# ---------------------------------------------------------------------------
# Test 5: nova seal sign --backend sigstore exits 1 when sigstore not installed
# ---------------------------------------------------------------------------

class TestSealSignCliSigstoreMissing:
    def test_seal_sign_sigstore_backend_exits_1_when_not_installed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """nova seal sign --backend sigstore exits 1 with install message when sigstore absent."""
        # Write a minimal manifest file
        manifest = tmp_path / "manifest.json"
        manifest.write_text('{"capsule_id": "test-123"}', encoding="utf-8")

        # Remove sigstore from sys.modules so the import check fails
        monkeypatch.setitem(sys.modules, "sigstore", None)  # type: ignore[arg-type]

        result = runner.invoke(
            app,
            ["seal", "sign", str(manifest), "--backend", "sigstore"],
        )

        assert result.exit_code == 1
        assert "novafabric[sigstore]" in result.output

    def test_seal_sign_sigstore_error_message_is_actionable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Error message must include pip install instruction."""
        manifest = tmp_path / "manifest.json"
        manifest.write_text('{"capsule_id": "test"}', encoding="utf-8")
        monkeypatch.setitem(sys.modules, "sigstore", None)  # type: ignore[arg-type]

        result = runner.invoke(
            app,
            ["seal", "sign", str(manifest), "--backend", "sigstore"],
        )

        assert result.exit_code == 1
        # Must contain actionable install instruction
        combined_output = result.output + (result.stderr if hasattr(result, "stderr") else "")
        assert "pip install" in combined_output or "pip install" in result.output

    def test_seal_sign_unknown_backend_exits_1(
        self, tmp_path: Path
    ) -> None:
        """nova seal sign --backend unknown exits 1 with error."""
        manifest = tmp_path / "manifest.json"
        manifest.write_text('{"capsule_id": "test"}', encoding="utf-8")

        result = runner.invoke(
            app,
            ["seal", "sign", str(manifest), "--backend", "badbackend"],
        )
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Test 6: nova seal sign --help includes --backend
# ---------------------------------------------------------------------------

class TestSealSignHelp:
    def test_seal_sign_help_includes_backend_option(self) -> None:
        """nova seal sign --help must include the --backend flag."""
        result = runner.invoke(app, ["seal", "sign", "--help"])
        assert result.exit_code == 0
        assert "--backend" in result.output

    def test_nova_verify_help_includes_backend_option(self) -> None:
        """nova verify --help must include the --backend flag."""
        result = runner.invoke(app, ["verify", "--help"])
        assert result.exit_code == 0
        assert "--backend" in result.output

    def test_seal_sign_help_mentions_sigstore(self) -> None:
        """nova seal sign --help must mention 'sigstore'."""
        result = runner.invoke(app, ["seal", "sign", "--help"])
        assert result.exit_code == 0
        assert "sigstore" in result.output.lower()


# ---------------------------------------------------------------------------
# Test 7: SigstoreVerificationResult with valid=False reports error correctly
# ---------------------------------------------------------------------------

class TestSigstoreVerificationResult:
    def test_invalid_result_str_includes_error(self) -> None:
        """SigstoreVerificationResult(valid=False) __str__ includes the error field."""
        result = SigstoreVerificationResult(
            valid=False,
            identity=None,
            rekor_log_index=None,
            error="Signature mismatch: certificate revoked",
        )
        s = str(result)
        assert "valid=False" in s
        assert "Signature mismatch" in s

    def test_valid_result_str_includes_identity_and_log_index(self) -> None:
        """SigstoreVerificationResult(valid=True) __str__ includes identity and log index."""
        result = SigstoreVerificationResult(
            valid=True,
            identity="user@example.com",
            rekor_log_index=12345,
            error=None,
        )
        s = str(result)
        assert "valid=True" in s
        assert "user@example.com" in s
        assert "12345" in s

    def test_invalid_result_has_false_valid_flag(self) -> None:
        """SigstoreVerificationResult with error has valid=False."""
        result = SigstoreVerificationResult(
            valid=False,
            identity=None,
            rekor_log_index=None,
            error="Rekor unavailable",
        )
        assert result.valid is False
        assert result.error == "Rekor unavailable"

    def test_valid_result_has_no_error(self) -> None:
        """SigstoreVerificationResult with valid=True has error=None."""
        result = SigstoreVerificationResult(
            valid=True,
            identity="ci@github.com",
            rekor_log_index=999,
            error=None,
        )
        assert result.valid is True
        assert result.error is None

    def test_verify_bundle_returns_invalid_result_when_sigstore_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """verify_bundle() returns SigstoreVerificationResult(valid=False) when sigstore absent."""
        monkeypatch.setitem(sys.modules, "sigstore", None)  # type: ignore[arg-type]
        monkeypatch.setitem(sys.modules, "sigstore.verify", None)  # type: ignore[arg-type]
        monkeypatch.setitem(sys.modules, "sigstore.models", None)  # type: ignore[arg-type]

        signer = SigstoreSigner()
        result = signer.verify_bundle(_SAMPLE_BUNDLE, b"artifact")

        assert isinstance(result, SigstoreVerificationResult)
        assert result.valid is False
        assert result.error is not None
        assert "novafabric[sigstore]" in result.error


# ---------------------------------------------------------------------------
# Test: SigstoreBundleStore.store_bundle / load_bundle capsule_id isolation
# ---------------------------------------------------------------------------

class TestBundleStoreIsolation:
    def test_different_capsule_ids_have_separate_files(self, tmp_path: Path) -> None:
        """store_bundle() keeps each capsule_id in a separate file."""
        SigstoreBundleStore.store_bundle("capsule-A", {"id": "A"}, tmp_path)
        SigstoreBundleStore.store_bundle("capsule-B", {"id": "B"}, tmp_path)

        a = SigstoreBundleStore.load_bundle("capsule-A", tmp_path)
        b = SigstoreBundleStore.load_bundle("capsule-B", tmp_path)

        assert a is not None and a["id"] == "A"
        assert b is not None and b["id"] == "B"

    def test_overwrite_bundle_returns_latest(self, tmp_path: Path) -> None:
        """Calling store_bundle twice for same capsule_id overwrites the first bundle."""
        SigstoreBundleStore.store_bundle("cap", {"version": 1}, tmp_path)
        SigstoreBundleStore.store_bundle("cap", {"version": 2}, tmp_path)

        loaded = SigstoreBundleStore.load_bundle("cap", tmp_path)
        assert loaded is not None
        assert loaded["version"] == 2
