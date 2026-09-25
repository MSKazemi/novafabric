"""ADR-0055 trust-resolution step 2 — offline CA-bundle chain validation (experimental).

Acceptance criteria:

* A leaf issued by an intermediate chains to a root in the operator ``ca_bundle``.
* Pinned-fingerprint behaviour is unchanged; trust is "pinned OR chain", pinned first.
* Fail closed on: expired leaf/intermediate, not-yet-valid leaf, wrong CA, missing
  intermediate, a CA certificate used as the signer, a tampered payload, and a tampered
  (re-signed) certificate.
* Malformed/empty CA bundles and DSSE envelopes raise ``X509ChainError`` (never a
  silent downgrade); chain validation itself never raises.
* ``verify_dsse_signer_chain`` binds signature to certificate: it passes only when a
  signature entry verifies under its *own* certificate's key and that certificate
  chains to the bundle. Both reviewer PoCs (attacker ``pubkey`` + legit ``cert``;
  legit ``cert`` with a garbage sig next to a valid attacker entry) fail closed.
* ``novaseal.yaml`` accepts an optional ``ca_bundle`` key; a set-but-missing path is a
  config error.
"""

from __future__ import annotations

import base64
import datetime
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import Encoding

from novafabric.trust.novaseal.config import SealConfigError, _parse_profile
from novafabric.trust.novaseal.x509_identity import (
    TRUST_BASIS_CA_CHAIN,
    TRUST_BASIS_PINNED,
    X509ChainError,
    X509Signature,
    X509SigningIdentity,
    load_ca_bundle,
    signer_certificate_from_dsse,
    validate_certificate_chain,
    verify_dsse_signer_chain,
    verify_x509_signature,
)

from ._x509_pki import (
    NOW,
    Pki,
    attacker_entry,
    dsse_envelope,
    ecdsa_entry,
    make_cert,
    make_pki,
)

PAYLOAD = b"capsule-manifest-bytes"


@pytest.fixture(scope="module")
def pki() -> Pki:
    return make_pki()


def _sign(pki: Pki, payload: bytes = PAYLOAD) -> X509Signature:
    identity = X509SigningIdentity.from_pem(pki.leaf.key_pem, pki.leaf.cert_pem)
    return identity.sign(payload)


# ---------------------------------------------------------------------------
# validate_certificate_chain
# ---------------------------------------------------------------------------


class TestValidateCertificateChain:
    def test_full_chain_validates(self, pki: Pki) -> None:
        result = validate_certificate_chain(
            pki.leaf.cert, [pki.root.cert], intermediates=[pki.intermediate.cert]
        )
        assert result.valid is True
        assert result.chain_subjects == (
            "CN=novaseal-signer",
            "CN=novaseal-intermediate-ca",
            "CN=novaseal-root-ca",
        )
        assert result.trust_anchor_fingerprint is not None
        assert result.trust_anchor_fingerprint.startswith("sha256:")

    def test_intermediate_trusted_directly_as_anchor(self, pki: Pki) -> None:
        result = validate_certificate_chain(pki.leaf.cert, [pki.intermediate.cert])
        assert result.valid is True
        assert result.chain_subjects[-1] == "CN=novaseal-intermediate-ca"

    def test_missing_intermediate_fails(self, pki: Pki) -> None:
        result = validate_certificate_chain(pki.leaf.cert, [pki.root.cert])
        assert result.valid is False
        assert "chain validation failed" in result.reason
        assert result.chain_subjects == ()
        assert result.trust_anchor_fingerprint is None

    def test_wrong_ca_fails(self, pki: Pki) -> None:
        other = make_pki("other")
        result = validate_certificate_chain(
            pki.leaf.cert, [other.root.cert], intermediates=[pki.intermediate.cert]
        )
        assert result.valid is False

    def test_expired_leaf_fails(self, pki: Pki) -> None:
        expired = make_cert(
            "expired-signer",
            issuer=pki.intermediate,
            ca=False,
            not_before=NOW - datetime.timedelta(days=30),
            not_after=NOW - datetime.timedelta(days=1),
        )
        result = validate_certificate_chain(
            expired.cert, [pki.root.cert], intermediates=[pki.intermediate.cert]
        )
        assert result.valid is False

    def test_expired_leaf_validates_at_a_past_time(self, pki: Pki) -> None:
        expired = make_cert(
            "expired-signer",
            issuer=pki.intermediate,
            ca=False,
            not_before=NOW - datetime.timedelta(hours=12),
            not_after=NOW - datetime.timedelta(hours=1),
        )
        past = (NOW - datetime.timedelta(hours=6)).replace(tzinfo=None)  # naive = UTC
        result = validate_certificate_chain(
            expired.cert,
            [pki.root.cert],
            intermediates=[pki.intermediate.cert],
            validation_time=past,
        )
        assert result.valid is True

    def test_not_yet_valid_leaf_fails(self, pki: Pki) -> None:
        future = make_cert(
            "future-signer",
            issuer=pki.intermediate,
            ca=False,
            not_before=NOW + datetime.timedelta(days=1),
            not_after=NOW + datetime.timedelta(days=30),
        )
        result = validate_certificate_chain(
            future.cert, [pki.root.cert], intermediates=[pki.intermediate.cert]
        )
        assert result.valid is False

    def test_expired_intermediate_fails(self, pki: Pki) -> None:
        old_int = make_cert(
            "old-intermediate",
            issuer=pki.root,
            ca=True,
            not_before=NOW - datetime.timedelta(days=30),
            not_after=NOW - datetime.timedelta(days=1),
        )
        leaf = make_cert("signer", issuer=old_int, ca=False)
        result = validate_certificate_chain(
            leaf.cert, [pki.root.cert], intermediates=[old_int.cert]
        )
        assert result.valid is False

    def test_ca_certificate_as_signer_fails(self, pki: Pki) -> None:
        result = validate_certificate_chain(pki.intermediate.cert, [pki.root.cert])
        assert result.valid is False
        assert "must not be a CA" in result.reason

    def test_forged_leaf_with_same_issuer_name_fails(self, pki: Pki) -> None:
        # An attacker CA that copies the real intermediate's subject name.
        rogue_root = make_cert("novaseal-root-ca", issuer=None, ca=True)
        rogue_int = make_cert("novaseal-intermediate-ca", issuer=rogue_root, ca=True)
        forged = make_cert("novaseal-signer", issuer=rogue_int, ca=False)
        result = validate_certificate_chain(
            forged.cert, [pki.root.cert], intermediates=[pki.intermediate.cert]
        )
        assert result.valid is False

    def test_empty_anchor_list_fails_without_raising(self, pki: Pki) -> None:
        result = validate_certificate_chain(pki.leaf.cert, [])
        assert result.valid is False
        assert "no trust anchors" in result.reason

    def test_chain_deeper_than_limit_fails(self, pki: Pki) -> None:
        result = validate_certificate_chain(
            pki.leaf.cert,
            [pki.root.cert],
            intermediates=[pki.intermediate.cert],
            max_chain_depth=1,
        )
        # depth 1 still admits one intermediate; build a 3-intermediate chain instead
        assert result.valid is True
        i2 = make_cert("i2", issuer=pki.intermediate, ca=True)
        i3 = make_cert("i3", issuer=i2, ca=True)
        deep_leaf = make_cert("deep", issuer=i3, ca=False)
        deep = validate_certificate_chain(
            deep_leaf.cert,
            [pki.root.cert],
            intermediates=[pki.intermediate.cert, i2.cert, i3.cert],
            max_chain_depth=1,
        )
        assert deep.valid is False

    @pytest.mark.parametrize("depth", [0, 17])
    def test_out_of_range_depth_raises(self, pki: Pki, depth: int) -> None:
        with pytest.raises(ValueError, match="max_chain_depth"):
            validate_certificate_chain(pki.leaf.cert, [pki.root.cert], max_chain_depth=depth)


# ---------------------------------------------------------------------------
# verify_x509_signature — pinned OR chain
# ---------------------------------------------------------------------------


class TestVerifySignatureWithCaBundle:
    def test_chain_trusted_signature_verifies(self, pki: Pki) -> None:
        result = verify_x509_signature(
            PAYLOAD,
            _sign(pki),
            ca_bundle=[pki.root.cert],
            intermediates=[pki.intermediate.cert],
        )
        assert result.valid is True
        assert result.trust_basis == TRUST_BASIS_CA_CHAIN
        assert result.reason == "signature verified and certificate chains to a trusted CA"
        assert result.chain_subjects[0] == "CN=novaseal-signer"
        assert result.subject_common_name == "novaseal-signer"

    def test_pinned_wins_before_chain(self, pki: Pki) -> None:
        sig = _sign(pki)
        fp = X509SigningIdentity.from_pem(
            pki.leaf.key_pem, pki.leaf.cert_pem
        ).certificate_fingerprint
        wrong = make_pki("wrong")
        result = verify_x509_signature(
            PAYLOAD, sig, pinned_fingerprints={fp}, ca_bundle=[wrong.root.cert]
        )
        assert result.valid is True
        assert result.trust_basis == TRUST_BASIS_PINNED
        assert result.chain_subjects == []

    def test_chain_used_when_not_pinned(self, pki: Pki) -> None:
        result = verify_x509_signature(
            PAYLOAD,
            _sign(pki),
            pinned_fingerprints={"sha256:" + "00" * 32},
            ca_bundle=[pki.intermediate.cert],
        )
        assert result.valid is True
        assert result.trust_basis == TRUST_BASIS_CA_CHAIN

    def test_missing_intermediate_is_untrusted(self, pki: Pki) -> None:
        result = verify_x509_signature(PAYLOAD, _sign(pki), ca_bundle=[pki.root.cert])
        assert result.valid is False
        assert result.trust_basis is None
        assert "not trusted by the CA bundle" in result.reason
        assert "untrusted signer" in result.reason

    def test_wrong_ca_is_untrusted(self, pki: Pki) -> None:
        other = make_pki("other")
        result = verify_x509_signature(
            PAYLOAD,
            _sign(pki),
            ca_bundle=[other.root.cert],
            intermediates=[pki.intermediate.cert],
        )
        assert result.valid is False

    def test_tampered_payload_rejected_even_when_chain_valid(self, pki: Pki) -> None:
        result = verify_x509_signature(
            b"TAMPERED",
            _sign(pki),
            ca_bundle=[pki.root.cert],
            intermediates=[pki.intermediate.cert],
        )
        assert result.valid is False
        assert "does not verify" in result.reason

    def test_swapped_certificate_rejected(self, pki: Pki) -> None:
        # Signature made by one chain-valid leaf, certificate swapped for another
        # chain-valid leaf from the same CA: chain passes, signature must not.
        sibling = make_cert("sibling-signer", issuer=pki.intermediate, ca=False)
        sig = _sign(pki).model_copy(update={"certificate_pem": sibling.cert_pem.decode("utf-8")})
        result = verify_x509_signature(
            PAYLOAD,
            sig,
            ca_bundle=[pki.root.cert],
            intermediates=[pki.intermediate.cert],
        )
        assert result.valid is False
        assert "does not verify" in result.reason

    def test_unsupported_algorithm_with_chain(self, pki: Pki) -> None:
        sig = _sign(pki).model_copy(update={"algorithm": "rsa-pss-sha256"})
        result = verify_x509_signature(PAYLOAD, sig, ca_bundle=[pki.intermediate.cert])
        assert result.valid is False
        assert "unsupported algorithm" in result.reason

    def test_no_anchor_at_all_rejects(self, pki: Pki) -> None:
        result = verify_x509_signature(PAYLOAD, _sign(pki))
        assert result.valid is False
        assert "pinned trust set" in result.reason


# ---------------------------------------------------------------------------
# load_ca_bundle / signer_certificate_from_dsse
# ---------------------------------------------------------------------------


class TestLoaders:
    def test_load_concatenated_bundle(self, pki: Pki) -> None:
        certs = load_ca_bundle(pki.root.cert_pem + pki.intermediate.cert_pem)
        assert [c.subject for c in certs] == [
            pki.root.cert.subject,
            pki.intermediate.cert.subject,
        ]

    @pytest.mark.parametrize("raw", [b"", b"not a pem", b"-----BEGIN CERTIFICATE-----\nZZ"])
    def test_malformed_bundle_raises(self, raw: bytes) -> None:
        with pytest.raises(X509ChainError, match="could not load CA bundle"):
            load_ca_bundle(raw)

    def test_x509_chain_error_is_identity_error(self) -> None:
        from novafabric.trust.novaseal.x509_identity import X509IdentityError

        assert issubclass(X509ChainError, X509IdentityError)

    @pytest.mark.parametrize("urlsafe", [True, False])
    def test_extract_cert_from_dsse(self, pki: Pki, urlsafe: bool) -> None:
        der = pki.leaf.cert.public_bytes(Encoding.DER)
        enc = base64.urlsafe_b64encode if urlsafe else base64.b64encode
        env = {"signatures": [{"sig": "x", "cert": enc(der).decode().rstrip("=")}]}
        cert = signer_certificate_from_dsse(json.dumps(env).encode())
        assert cert == pki.leaf.cert

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            (b"", "not valid JSON"),
            (b"\xff\xfe", "not valid JSON"),
            (b"[]", "no signatures"),
            (b'{"signatures": []}', "no signatures"),
            (b'{"signatures": ["x"]}', "no signatures"),
            (b'{"signatures": [{"pubkey": "abc"}]}', "no X.509 certificate"),
            (b'{"signatures": [{"cert": "!!!"}]}', "unparseable"),
            (b'{"signatures": [{"cert": "AAAA"}]}', "unparseable"),
        ],
    )
    def test_bad_dsse_raises(self, raw: bytes, match: str) -> None:
        with pytest.raises(X509ChainError, match=match):
            signer_certificate_from_dsse(raw)


# ---------------------------------------------------------------------------
# verify_dsse_signer_chain — signature <-> certificate binding
# ---------------------------------------------------------------------------


class TestDsseSignerBinding:
    def test_genuine_envelope_passes(self, pki: Pki) -> None:
        env = dsse_envelope(PAYLOAD, [ecdsa_entry(pki.leaf, PAYLOAD)])
        result = verify_dsse_signer_chain(env, [pki.intermediate.cert])
        assert result.valid is True, result.reason
        assert result.chain_subjects[0] == "CN=novaseal-signer"

    def test_matching_pubkey_alongside_cert_passes(self, pki: Pki) -> None:
        from cryptography.hazmat.primitives.serialization import PublicFormat

        entry = ecdsa_entry(pki.leaf, PAYLOAD)
        pub = pki.leaf.cert.public_key().public_bytes(
            Encoding.PEM, PublicFormat.SubjectPublicKeyInfo
        )
        entry["pubkey"] = base64.b64encode(pub).decode()
        result = verify_dsse_signer_chain(
            dsse_envelope(PAYLOAD, [entry]), [pki.root.cert], intermediates=[pki.intermediate.cert]
        )
        assert result.valid is True, result.reason

    def test_poc_attacker_pubkey_with_legit_cert_fails(self, pki: Pki) -> None:
        """PoC 1: {pubkey: attacker, cert: legit leaf, sig: attacker over forged payload}."""
        from novafabric.trust.novaseal.envelope import verify_envelope

        forged = b'{"run_id": "forged"}'
        env = dsse_envelope(forged, [attacker_entry(forged, pki.leaf.cert)])
        # The envelope check alone accepts it (it verifies under ``pubkey``) ...
        assert verify_envelope(env) is True
        # ... so the chain check must not vouch for it.
        result = verify_dsse_signer_chain(env, [pki.intermediate.cert])
        assert result.valid is False
        assert "does not match the certificate" in result.reason

    def test_poc_legit_cert_garbage_sig_plus_valid_attacker_entry_fails(self, pki: Pki) -> None:
        """PoC 2: sig[0] legit cert + garbage sig; sig[1] attacker pubkey, valid sig."""
        from novafabric.trust.novaseal.envelope import verify_envelope

        forged = b'{"run_id": "forged"}'
        legit = ecdsa_entry(pki.leaf, forged)
        legit["sig"] = base64.b64encode(b"\x30\x06garbage").decode()
        env = dsse_envelope(forged, [legit, attacker_entry(forged, None)])
        assert verify_envelope(env, require="any") is True
        result = verify_dsse_signer_chain(env, [pki.intermediate.cert])
        assert result.valid is False
        assert "signatures[0]: signature does not verify" in result.reason
        assert "signatures[1]: no X.509 certificate" in result.reason

    def test_legit_cert_sig_by_other_key_fails(self, pki: Pki) -> None:
        other = make_cert("impostor", issuer=None, ca=False)
        entry = ecdsa_entry(other, PAYLOAD, cert=False)
        entry["cert"] = ecdsa_entry(pki.leaf, PAYLOAD)["cert"]
        result = verify_dsse_signer_chain(dsse_envelope(PAYLOAD, [entry]), [pki.intermediate.cert])
        assert result.valid is False
        assert "does not verify under the certificate" in result.reason

    def test_bound_but_untrusted_cert_fails(self, pki: Pki) -> None:
        rogue = make_cert("rogue", issuer=None, ca=False)
        env = dsse_envelope(PAYLOAD, [ecdsa_entry(rogue, PAYLOAD)])
        result = verify_dsse_signer_chain(env, [pki.intermediate.cert])
        assert result.valid is False
        assert "chain validation failed" in result.reason

    def test_second_entry_can_carry_trust(self, pki: Pki) -> None:
        env = dsse_envelope(
            PAYLOAD, [attacker_entry(PAYLOAD, None), ecdsa_entry(pki.leaf, PAYLOAD)]
        )
        result = verify_dsse_signer_chain(env, [pki.intermediate.cert])
        assert result.valid is True
        assert "signatures[1]" in result.reason

    @pytest.mark.parametrize(
        ("entry", "match"),
        [
            ("x", "not an object"),
            ({"cert": "!!!", "sig": ""}, "certificate is unparseable"),
            ({"cert": "AAAA", "sig": ""}, "certificate is unparseable"),
        ],
    )
    def test_malformed_entries_fail(self, pki: Pki, entry: object, match: str) -> None:
        raw = json.dumps({"payloadType": "t", "payload": "", "signatures": [entry]}).encode()
        result = verify_dsse_signer_chain(raw, [pki.root.cert])
        assert result.valid is False
        assert match in result.reason

    def test_bad_pubkey_and_sig_fields_fail(self, pki: Pki) -> None:
        good = ecdsa_entry(pki.leaf, PAYLOAD)
        for field, value, match in [
            ("pubkey", "AAAA", "'pubkey' is unparseable"),
            ("pubkey", 7, "'pubkey' is unparseable"),
            ("sig", 7, "signature is unparseable"),
            ("sig", "!!!", "signature is unparseable"),
        ]:
            entry = {**good, field: value}
            env = dsse_envelope(PAYLOAD, [entry])  # type: ignore[list-item]
            result = verify_dsse_signer_chain(env, [pki.intermediate.cert])
            assert result.valid is False
            assert match in result.reason, (field, value, result.reason)

    @pytest.mark.parametrize(
        ("raw", "match"),
        [
            (b"not json", "not valid JSON"),
            (b"[]", "no signatures"),
            (b'{"signatures": []}', "no signatures"),
            (b'{"signatures": [{}], "payload": 5}', "payload is unparseable"),
        ],
    )
    def test_unusable_envelope_raises(self, pki: Pki, raw: bytes, match: str) -> None:
        with pytest.raises(X509ChainError, match=match):
            verify_dsse_signer_chain(raw, [pki.root.cert])


# ---------------------------------------------------------------------------
# novaseal.yaml ca_bundle key
# ---------------------------------------------------------------------------


def _write_local_config(tmp_path: Path, pki: Pki, extra: str = "") -> Path:
    key = tmp_path / "k.pem"
    cert = tmp_path / "c.pem"
    key.write_bytes(pki.leaf.key_pem)
    cert.write_bytes(pki.leaf.cert_pem)
    cfg = tmp_path / "novaseal.yaml"
    cfg.write_text(
        f"profile: local\nkey_path: {key}\ncert_path: {cert}\n"
        f"merkle_db: {tmp_path / 'm.db'}\n{extra}"
    )
    return cfg


class TestConfigCaBundle:
    def test_absent_key_is_none(self, tmp_path: Path, pki: Pki) -> None:
        assert _parse_profile(_write_local_config(tmp_path, pki)).ca_bundle is None

    def test_present_key_parses(self, tmp_path: Path, pki: Pki) -> None:
        bundle = tmp_path / "ca.pem"
        bundle.write_bytes(pki.root.cert_pem)
        profile = _parse_profile(_write_local_config(tmp_path, pki, f"ca_bundle: {bundle}\n"))
        assert profile.ca_bundle == bundle

    def test_missing_file_is_error(self, tmp_path: Path, pki: Pki) -> None:
        cfg = _write_local_config(tmp_path, pki, f"ca_bundle: {tmp_path / 'nope.pem'}\n")
        with pytest.raises(SealConfigError, match="ca_bundle not found"):
            _parse_profile(cfg)

    @pytest.mark.parametrize("value", ['""', "42", "[a, b]"])
    def test_non_string_is_error(self, tmp_path: Path, pki: Pki, value: str) -> None:
        cfg = _write_local_config(tmp_path, pki, f"ca_bundle: {value}\n")
        with pytest.raises(SealConfigError, match="non-empty path string"):
            _parse_profile(cfg)

    def test_other_profile_carries_ca_bundle(self, tmp_path: Path, pki: Pki) -> None:
        cert = tmp_path / "c.pem"
        cert.write_bytes(pki.leaf.cert_pem)
        bundle = tmp_path / "ca.pem"
        bundle.write_bytes(pki.root.cert_pem)
        cfg = tmp_path / "novaseal.yaml"
        cfg.write_text(
            f"profile: gcp_kms\nkey_version_name: projects/p/k/1\ncert_path: {cert}\n"
            f"merkle_db: {tmp_path / 'm.db'}\nca_bundle: {bundle}\n"
        )
        assert _parse_profile(cfg).ca_bundle == bundle
