"""Tests for offline token subsystem (Track S-3).

Covers:
- Keypair generation
- Issue token → validates without OIDC provider
- Revoked token → PermissionError
- Expired token → jwt.ExpiredSignatureError
- Token audit table persistence
"""

from __future__ import annotations

from pathlib import Path

import jwt
import pytest

from novafabric.server.offline_tokens import (
    generate_keypair,
    issue_token,
    revoke_token,
    verify_offline_token,
)

# ---------------------------------------------------------------------------
# Keypair generation
# ---------------------------------------------------------------------------


class TestGenerateKeypair:
    def test_creates_private_and_public_files(self, tmp_path: Path) -> None:
        priv = tmp_path / "keys" / "offline.pem"
        priv_out, pub_out = generate_keypair(priv)

        assert priv_out.exists()
        assert pub_out.exists()
        assert pub_out.suffix == ".pub"

    def test_private_key_mode_0600(self, tmp_path: Path) -> None:
        import stat

        priv = tmp_path / "offline.pem"
        generate_keypair(priv)
        mode = stat.S_IMODE(priv.stat().st_mode)
        assert mode == 0o600

    def test_valid_pem_content(self, tmp_path: Path) -> None:
        priv = tmp_path / "offline.pem"
        generate_keypair(priv)
        content = priv.read_text()
        assert "PRIVATE KEY" in content


# ---------------------------------------------------------------------------
# Issue and verify
# ---------------------------------------------------------------------------


class TestIssueAndVerify:
    def test_issued_token_validates(self, tmp_path: Path) -> None:
        priv = tmp_path / "offline.pem"
        generate_keypair(priv)

        token = issue_token(
            subject="user@example.com",
            roles=["reader", "writer"],
            expires_in_days=1,
            key_path=priv,
            db_path=tmp_path / "tokens.db",
        )

        ctx = verify_offline_token(
            token, priv, db_path=tmp_path / "tokens.db"
        )
        assert ctx.subject == "user@example.com"
        assert "reader" in ctx.roles
        assert "writer" in ctx.roles

    def test_token_contains_expected_claims(self, tmp_path: Path) -> None:
        priv = tmp_path / "offline.pem"
        generate_keypair(priv)

        token = issue_token(
            subject="svc@example.com",
            roles=["admin"],
            expires_in_days=30,
            key_path=priv,
            db_path=tmp_path / "tokens.db",
        )

        # Decode without verification to inspect claims
        claims = jwt.decode(token, options={"verify_signature": False})
        assert claims["sub"] == "svc@example.com"
        assert claims["nova_roles"] == ["admin"]
        assert claims["iss"] == "nova-offline"
        assert "jti" in claims
        assert "exp" in claims


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


class TestRevocation:
    def test_revoked_token_raises_permission_error(self, tmp_path: Path) -> None:
        priv = tmp_path / "offline.pem"
        generate_keypair(priv)
        db = tmp_path / "tokens.db"

        token = issue_token(
            subject="user@example.com",
            roles=["reader"],
            expires_in_days=1,
            key_path=priv,
            db_path=db,
        )

        # Extract token_id from the JWT claims
        claims = jwt.decode(token, options={"verify_signature": False})
        token_id = claims["jti"]

        revoke_token(token_id, priv, db_path=db)

        with pytest.raises(PermissionError, match="revoked"):
            verify_offline_token(token, priv, db_path=db)

    def test_revoke_nonexistent_token_raises_key_error(self, tmp_path: Path) -> None:
        priv = tmp_path / "offline.pem"
        generate_keypair(priv)
        db = tmp_path / "tokens.db"
        # Ensure the DB exists with the table
        issue_token("x", ["reader"], 1, priv, db_path=db)

        with pytest.raises(KeyError, match="not found"):
            revoke_token("nonexistent-id", priv, db_path=db)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


class TestExpiry:
    def test_expired_token_raises(self, tmp_path: Path) -> None:
        """Issue a token that expires in the past using the correct keypair."""
        from datetime import datetime, timedelta, timezone

        import jwt as _jwt
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
        )

        db = tmp_path / "tokens.db"

        # Generate a fresh keypair and write to the canonical paths
        priv = tmp_path / "offline.pem"
        private_key = Ed25519PrivateKey.generate()
        priv_pem = private_key.private_bytes(
            encoding=Encoding.PEM, format=PrivateFormat.PKCS8, encryption_algorithm=NoEncryption()
        )
        pub_pem = private_key.public_key().public_bytes(
            encoding=Encoding.PEM, format=PublicFormat.SubjectPublicKeyInfo
        )
        priv.write_bytes(priv_pem)
        priv.chmod(0o600)
        priv.with_suffix(".pub").write_bytes(pub_pem)

        import uuid

        now = datetime.now(timezone.utc)
        expired_at = now - timedelta(seconds=30)
        payload = {
            "jti": str(uuid.uuid4()),
            "sub": "user@example.com",
            "nova_roles": ["reader"],
            "iat": int(now.timestamp()),
            "exp": int(expired_at.timestamp()),
            "iss": "nova-offline",
        }
        expired_token = _jwt.encode(payload, private_key, algorithm="EdDSA")

        with pytest.raises(_jwt.ExpiredSignatureError):
            verify_offline_token(expired_token, priv, db_path=db)


# ---------------------------------------------------------------------------
# Server-side OIDC + offline token integration
# ---------------------------------------------------------------------------


class TestOfflineTokenEndpoint:
    """Test that offline tokens can authenticate against the server in no-auth mode.

    Full OIDC integration with offline tokens requires a custom auth flow beyond
    the standard JWKS path.  This test verifies the token round-trip at the
    unit level (issue → verify) as the server integration depends on a custom
    verify_token override that is out of scope for the base server middleware.
    """

    def test_token_round_trip(self, tmp_path: Path) -> None:
        priv = tmp_path / "offline.pem"
        generate_keypair(priv)
        db = tmp_path / "tokens.db"

        token = issue_token("alice@example.com", ["admin"], 7, priv, db_path=db)
        ctx = verify_offline_token(token, priv, db_path=db)
        assert ctx.subject == "alice@example.com"
        assert ctx.has_role("admin")

    def test_public_key_only_needed_for_verification(self, tmp_path: Path) -> None:
        """Verify works with only the .pub file present (private key may be absent)."""
        priv = tmp_path / "offline.pem"
        generate_keypair(priv)
        db = tmp_path / "tokens.db"

        token = issue_token("bob@example.com", ["reader"], 1, priv, db_path=db)

        # Remove private key — only .pub file remains
        priv.unlink()

        # verify_offline_token loads from .pub file → should succeed
        ctx = verify_offline_token(token, priv, db_path=db)
        assert ctx.subject == "bob@example.com"
