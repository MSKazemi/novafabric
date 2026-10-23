"""L15 — the ADR-0060 last-admin guard must ask the SERVER whether OIDC is configured.

ADR-0060's invariant: refuse a revoke that would leave zero ``admin`` rows in
``role_assignments`` AND no OIDC issuer (JWT-claim admins can still log in when
there is one). ``rbac_store.revoke_role`` decided "OIDC configured" from the env
var ``NOVA_OIDC_ISSUER`` — which server-mode OIDC never reads (it is the YAML
``oidc:`` block; there are no OIDC env vars). So:

* a real OIDC server (YAML) refused a revoke the ADR allows (false 409), and
* any process with a stray ``NOVA_OIDC_ISSUER`` exported silently DISABLED the
  lockout guard, on a server with no OIDC at all.

The fact is now an explicit input taken from the server's own config, and the
default is "not configured", so a caller that does not know keeps the guard on.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import jwt
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from novafabric.cli.main import app as cli_app  # noqa: E402
from novafabric.server import rbac_store  # noqa: E402
from novafabric.server.app import create_app  # noqa: E402
from novafabric.server.auth import JwksCache  # noqa: E402
from novafabric.server.config import OidcConfig, ServerConfig  # noqa: E402
from novafabric.server.rbac_store import LastAdminError  # noqa: E402

ISSUER = "https://oidc.example.com"
AUD = "nova-server"


# --------------------------------------------------------------------------- #
# Store layer
# --------------------------------------------------------------------------- #


def test_env_var_no_longer_disables_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "r.db"
    monkeypatch.setenv("NOVA_OIDC_ISSUER", ISSUER)
    rbac_store.assign_role("solo@example.com", "admin", "t", db_path=db)
    with pytest.raises(LastAdminError):
        rbac_store.revoke_role("solo@example.com", "admin", db_path=db)
    assert rbac_store.get_roles("solo@example.com", db_path=db) == ["admin"]


def test_configured_oidc_allows_revoking_the_last_admin_row(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    rbac_store.assign_role("solo@example.com", "admin", "t", db_path=db)
    assert rbac_store.revoke_role(
        "solo@example.com", "admin", db_path=db, oidc_configured=True
    )


# --------------------------------------------------------------------------- #
# Production server: the route reads app.state.config.oidc
# --------------------------------------------------------------------------- #


def _rsa() -> tuple[Any, dict[str, Any]]:
    import json

    from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
    from jwt.algorithms import RSAAlgorithm

    key = generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update({"kid": "l15", "alg": "RS256", "use": "sig"})
    return key, jwk


def _bearer(key: Any) -> dict[str, str]:
    now = int(time.time())
    token = jwt.encode(
        {"sub": "admin@example.com", "aud": AUD, "iss": ISSUER, "iat": now,
         "exp": now + 600, "nova_roles": ["admin"]},
        key, algorithm="RS256", headers={"kid": "l15"},
    )
    return {"Authorization": f"Bearer {token}"}


def test_oidc_server_from_yaml_config_allows_the_revoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NOVA_OIDC_ISSUER", raising=False)  # the server never reads it
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    key, jwk = _rsa()

    def fake_fetch(self: JwksCache) -> None:
        self._keys = {jwk["kid"]: jwk}
        self._fetched_at = time.monotonic()

    import novafabric.server.auth as auth_mod

    auth_mod._jwks_cache = None
    cfg = ServerConfig(
        db_path=str(tmp_path / "srv.db"), oidc=OidcConfig(issuer_url=ISSUER, audience=AUD)
    )
    with patch.object(JwksCache, "_fetch", fake_fetch):
        client = TestClient(create_app(cfg), raise_server_exceptions=False)
        client.post("/v0/admin/roles", json={"subject": "solo@example.com", "role": "admin"},
                    headers=_bearer(key))
        resp = client.delete("/v0/admin/roles/solo@example.com/admin", headers=_bearer(key))
    assert resp.status_code == 200, resp.text


def test_server_without_oidc_still_refuses_with_the_env_var_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_OIDC_ISSUER", ISSUER)  # stray, must not bypass
    monkeypatch.setenv("NOVAFABRIC_DASHBOARD_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    cfg = ServerConfig(db_path=str(tmp_path / "srv.db"), insecure_no_auth=True)
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    client.post("/v0/admin/roles", json={"subject": "solo@example.com", "role": "admin"})
    resp = client.delete("/v0/admin/roles/solo@example.com/admin")
    assert resp.status_code == 409, resp.text
    assert "last admin" in resp.json()["detail"].lower()


# --------------------------------------------------------------------------- #
# CLI: reads the same server config the server would start with
# --------------------------------------------------------------------------- #


def test_cli_revoke_reads_oidc_from_the_server_config_file(tmp_path: Path) -> None:
    db = tmp_path / "r.db"
    rbac_store.assign_role("solo@example.com", "admin", "t", db_path=db)
    conf = tmp_path / "server.yaml"
    conf.write_text(f"oidc:\n  issuer_url: {ISSUER}\n  audience: {AUD}\n")
    result = CliRunner().invoke(
        cli_app,
        ["server", "revoke-role", "solo@example.com", "admin", "--db-path", str(db),
         "--config", str(conf)],
    )
    assert result.exit_code == 0, result.output


def test_cli_revoke_without_oidc_config_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVA_OIDC_ISSUER", ISSUER)
    db = tmp_path / "r.db"
    rbac_store.assign_role("solo@example.com", "admin", "t", db_path=db)
    conf = tmp_path / "server.yaml"
    conf.write_text("backend: sqlite\n")
    result = CliRunner().invoke(
        cli_app,
        ["server", "revoke-role", "solo@example.com", "admin", "--db-path", str(db),
         "--config", str(conf)],
    )
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 2
    assert "last admin" in result.output.lower()
