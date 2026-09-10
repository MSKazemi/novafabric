"""Coverage-focused CLI tests for verify / server / serve / lineage-store.

These exercise error paths and command bodies that other suites don't reach.
External services (uvicorn, Postgres/RBAC, OIDC/HTTP, OCS) are mocked — no
real server is started and no port is bound.
"""

from __future__ import annotations

import json
import os

import pytest
import typer
from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


class _FakeApp:
    """Stand-in for a FastAPI app so serve_cmd can run without a real server.

    Supports the three operations serve_cmd performs: include_router, the "/"
    StaticFiles mount, and attribute storage on .state.
    """

    def __init__(self) -> None:
        self.state = type("State", (), {})()
        self.included: list[object] = []
        self.mounts: list[str] = []

    def include_router(self, router: object) -> None:
        self.included.append(router)

    def mount(self, path: str, app: object, name: str | None = None) -> None:
        self.mounts.append(path)


# ---------------------------------------------------------------------------
# nova verify — local backend happy path (covers the seal/Merkle body)
# ---------------------------------------------------------------------------


@pytest.fixture()
def sealed_capsule(tmp_path):
    """Create a capsule dir with a valid .seal/ bundle. Returns (dir, config)."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from novafabric.trust.novaseal import KeyConfig, NovaSeal

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "seal.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-Cov")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "seal.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    merkle_db = tmp_path / "merkle.db"
    config_path = tmp_path / "novaseal.yaml"
    config_path.write_text(
        f"profile: local\n"
        f"key_path: {key_path}\n"
        f"cert_path: {cert_path}\n"
        f"tsa_url: \n"
        f"merkle_db: {merkle_db}\n"
    )

    config = KeyConfig(
        profile="local", key_path=str(key_path), cert_path=str(cert_path)
    )
    seal = NovaSeal(config=config, tsa_url="", db_path=str(merkle_db))

    capsule_dir = tmp_path / "cov-capsule"
    capsule_dir.mkdir()
    bundle = seal.seal({"run_id": "cov-001", "status": "success"})
    seal_dir = capsule_dir / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(
        json.dumps(bundle.log_entry, indent=2), encoding="utf-8"
    )
    return capsule_dir, config_path


class TestVerifyLocalBackend:
    def test_local_backend_success(self, sealed_capsule):
        capsule_dir, config_path = sealed_capsule
        result = runner.invoke(
            app, ["verify", str(capsule_dir), "--seal-config", str(config_path)]
        )
        assert result.exit_code == 0, result.output
        assert "signature_ok=True" in result.output

    def test_local_backend_corrupt_log_entry_tolerated(self, sealed_capsule):
        # A malformed log-entry.json triggers the except-pass branch (171-172).
        capsule_dir, config_path = sealed_capsule
        (capsule_dir / ".seal" / "log-entry.json").write_text("not-json{")
        result = runner.invoke(
            app, ["verify", str(capsule_dir), "--seal-config", str(config_path)]
        )
        # Verification still runs (capsule_id falls back to "") and reports.
        assert "NovaSeal verification" in result.output


# ---------------------------------------------------------------------------
# nova verify — uncovered error/branch paths
# ---------------------------------------------------------------------------


class TestVerifyBranches:
    def test_local_missing_capsule_dir_exits_1(self, tmp_path):
        result = runner.invoke(app, ["verify", str(tmp_path / "nope")])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_local_no_seal_dir_exits_1(self, tmp_path):
        cap = tmp_path / "cap"
        cap.mkdir()
        result = runner.invoke(app, ["verify", str(cap)])
        assert result.exit_code == 1
        assert ".seal" in result.output

    def test_local_profile_none_exits_1(self, tmp_path, monkeypatch):
        cap = tmp_path / "cap"
        cap.mkdir()
        (cap / ".seal").mkdir()
        monkeypatch.setattr(
            "novafabric.trust.novaseal.config.load_signing_profile",
            lambda: None,
        )
        result = runner.invoke(app, ["verify", str(cap)])
        assert result.exit_code == 1
        assert "not configured" in result.output.lower()

    def test_unknown_backend_exits_1(self, tmp_path):
        cap = tmp_path / "cap"
        cap.mkdir()
        result = runner.invoke(app, ["verify", str(cap), "--backend", "bogus"])
        assert result.exit_code == 1
        assert "Unknown backend" in result.output

    def test_check_redaction_missing_file_exits_1(self, tmp_path):
        cap = tmp_path / "cap"
        cap.mkdir()
        missing = tmp_path / "report.seal.json"
        result = runner.invoke(
            app, ["verify", str(cap), "--check-redaction", str(missing)]
        )
        assert result.exit_code == 1
        assert "seal file not found" in result.output

    def test_check_redaction_valid(self, tmp_path, monkeypatch):
        cap = tmp_path / "cap"
        cap.mkdir()
        seal_file = tmp_path / "report.seal.json"
        seal_file.write_bytes(b'{"some": "envelope"}')

        monkeypatch.setattr(
            "novafabric.trust.novaseal.envelope.verify_envelope",
            lambda b: True,
        )
        monkeypatch.setattr(
            "novafabric.trust.novaseal.envelope.extract_intent",
            lambda b: None,
        )
        result = runner.invoke(
            app, ["verify", str(cap), "--check-redaction", str(seal_file)]
        )
        assert result.exit_code == 0
        assert "VALID" in result.output

    def test_check_redaction_invalid(self, tmp_path, monkeypatch):
        cap = tmp_path / "cap"
        cap.mkdir()
        seal_file = tmp_path / "report.seal.json"
        seal_file.write_bytes(b'{"bad": "envelope"}')

        def _boom(b):
            raise ValueError("bad signature")

        monkeypatch.setattr(
            "novafabric.trust.novaseal.envelope.verify_envelope", _boom
        )
        result = runner.invoke(
            app, ["verify", str(cap), "--check-redaction", str(seal_file)]
        )
        assert result.exit_code == 1
        assert "INVALID" in result.output

    def test_sigstore_backend_no_bundle_exits_1(self, tmp_path, monkeypatch):
        cap = tmp_path / "cap"
        cap.mkdir()
        (cap / "manifest.json").write_bytes(b'{"run_id": "x"}')

        # No bundle on disk → graceful exit 1.
        monkeypatch.setattr(
            "novafabric.trust.novaseal.sigstore_signer.SigstoreBundleStore.load_bundle",
            staticmethod(lambda cid, home: None),
        )
        result = runner.invoke(
            app,
            ["verify", str(cap), "--backend", "sigstore", "--home", str(tmp_path)],
        )
        assert result.exit_code == 1
        assert "No Sigstore bundle" in result.output

    def test_sigstore_backend_no_capsule_id_exits_1(self, tmp_path):
        # Empty capsule dir (no manifest) and no --capsule-id → cannot resolve id.
        empty = tmp_path / "empty"
        empty.mkdir()
        result = runner.invoke(
            app, ["verify", str(empty), "--backend", "sigstore", "--home", str(tmp_path)]
        )
        assert result.exit_code == 1
        assert "Cannot determine capsule ID" in result.output

    def test_sigstore_backend_verifies_bundle(self, tmp_path, monkeypatch):
        cap = tmp_path / "cap"
        cap.mkdir()
        (cap / "manifest.json").write_bytes(b'{"run_id": "x"}')

        monkeypatch.setattr(
            "novafabric.trust.novaseal.sigstore_signer.SigstoreBundleStore.load_bundle",
            staticmethod(lambda cid, home: {"bundle": "data"}),
        )

        class _Result:
            valid = True
            identity = "alice@example.com"
            rekor_log_index = 42
            error = None

            def __str__(self) -> str:
                return "valid=True"

        class _Signer:
            def verify_bundle(self, bundle, artifact):
                return _Result()

        monkeypatch.setattr(
            "novafabric.trust.novaseal.sigstore_signer.SigstoreSigner",
            _Signer,
        )
        result = runner.invoke(
            app,
            [
                "verify",
                str(cap),
                "--backend",
                "sigstore",
                "--capsule-id",
                "cid-1",
                "--home",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Identity: alice@example.com" in result.output
        assert "Rekor log index: 42" in result.output


# ---------------------------------------------------------------------------
# nova server — uncovered subcommand bodies (mock all external infra)
# ---------------------------------------------------------------------------


class TestServerStart:
    def test_start_runs_uvicorn_mocked(self, tmp_path, monkeypatch):
        calls = {}

        from novafabric.server.config import TlsConfig

        class _Cfg:
            host = "127.0.0.1"
            port = 7433
            backend = "sqlite"
            # ADR-0184 fields read by `nova server start`
            insecure_no_auth = False
            i_know_this_is_public = False
            local_token = None
            tls = TlsConfig()  # ADR-0241: read by _ssl_paths at launch

            class oidc:
                enabled = False

        monkeypatch.setattr(
            "novafabric.server.config.load_config", lambda c: _Cfg()
        )
        monkeypatch.setattr(
            "novafabric.server.app.create_app", lambda cfg: object()
        )

        import uvicorn

        def _fake_run(app_obj, host, port, **kwargs):
            calls["host"] = host
            calls["port"] = port

        monkeypatch.setattr(uvicorn, "run", _fake_run)

        result = runner.invoke(
            app, ["server", "start", "--host", "0.0.0.0", "--port", "8080"]
        )
        assert result.exit_code == 0, result.output
        assert calls == {"host": "0.0.0.0", "port": 8080}
        assert "Starting NovaFabric server" in result.output


class TestServerStartWorkers:
    def _cfg(self, backend: str):
        from novafabric.server.config import TlsConfig

        class _Cfg:
            host = "127.0.0.1"
            port = 7433
            insecure_no_auth = False
            i_know_this_is_public = False
            i_accept_shared_capsule_store = False
            local_token = None
            tls = TlsConfig()  # ADR-0241: read by _ssl_paths at launch

            class oidc:
                enabled = True  # skip the local-token block in the parent

        _Cfg.backend = backend
        return _Cfg()

    def test_workers_gt_one_requires_postgres(self, monkeypatch):
        monkeypatch.setattr(
            "novafabric.server.config.load_config",
            lambda c: self._cfg("sqlite"),
        )
        result = runner.invoke(app, ["server", "start", "--workers", "2"])
        assert result.exit_code == 2, result.output
        assert "requires --backend postgres" in result.output

    def test_workers_gt_one_launches_factory(self, monkeypatch):
        monkeypatch.setattr(
            "novafabric.server.config.load_config",
            lambda c: self._cfg("postgres"),
        )
        captured = {}

        import uvicorn

        def _fake_run(target, **kwargs):
            captured["target"] = target
            captured.update(kwargs)

        monkeypatch.setattr(uvicorn, "run", _fake_run)

        result = runner.invoke(app, ["server", "start", "--workers", "3"])
        assert result.exit_code == 0, result.output

        from novafabric.server.factory import FACTORY_TARGET

        assert captured["target"] == FACTORY_TARGET
        assert captured["factory"] is True
        assert captured["workers"] == 3
        # Effective config is exported for the worker processes.
        assert os.environ["NOVAFABRIC_SERVER_BACKEND"] == "postgres"
        assert os.environ["NOVAFABRIC_SERVER_PORT"] == "7433"


class TestServerIssueToken:
    def test_issue_token_generates_and_prints(self, tmp_path, monkeypatch):
        key_path = tmp_path / "offline-key.pem"

        monkeypatch.setattr(
            "novafabric.server.offline_tokens.generate_keypair",
            lambda p: (p, p.with_suffix(".pub")),
        )
        monkeypatch.setattr(
            "novafabric.server.offline_tokens.issue_token",
            lambda subject, roles, expires_in_days, key_path: "TOKEN.JWT.SIG",
        )
        result = runner.invoke(
            app,
            [
                "server",
                "issue-token",
                "--subject",
                "alice@example.com",
                "--roles",
                "reader,writer",
                "--expires-in",
                "30d",
                "--key-path",
                str(key_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Generating new keypair" in result.output
        assert "TOKEN.JWT.SIG" in result.output

    def test_issue_token_failure_exits_1(self, tmp_path, monkeypatch):
        key_path = tmp_path / "k.pem"
        key_path.write_bytes(b"exists")  # so generate is skipped

        def _boom(**kw):
            raise RuntimeError("crypto fail")

        monkeypatch.setattr(
            "novafabric.server.offline_tokens.issue_token", _boom
        )
        result = runner.invoke(
            app,
            ["server", "issue-token", "--subject", "x", "--key-path", str(key_path)],
        )
        assert result.exit_code == 1
        assert "Failed to issue token" in result.output


class TestServerRevokeToken:
    def test_revoke_token_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "novafabric.server.offline_tokens.revoke_token",
            lambda token_id, key: None,
        )
        result = runner.invoke(
            app, ["server", "revoke-token", "jti-123", "--key-path", str(tmp_path / "k.pem")]
        )
        assert result.exit_code == 0, result.output
        assert "revoked" in result.output

    def test_revoke_token_keyerror_exits_1(self, tmp_path, monkeypatch):
        def _boom(token_id, key):
            raise KeyError("no such token")

        monkeypatch.setattr(
            "novafabric.server.offline_tokens.revoke_token", _boom
        )
        result = runner.invoke(
            app, ["server", "revoke-token", "jti-x", "--key-path", str(tmp_path / "k.pem")]
        )
        assert result.exit_code == 1

    def test_revoke_token_generic_error_exits_1(self, tmp_path, monkeypatch):
        def _boom(token_id, key):
            raise RuntimeError("io error")

        monkeypatch.setattr(
            "novafabric.server.offline_tokens.revoke_token", _boom
        )
        result = runner.invoke(
            app, ["server", "revoke-token", "jti-x", "--key-path", str(tmp_path / "k.pem")]
        )
        assert result.exit_code == 1
        assert "Failed to revoke token" in result.output


class TestServerAssignRole:
    def test_assign_role_success(self, tmp_path, monkeypatch):
        recorded = {}

        def _assign(user, role, assigned_by, db_path=None):
            recorded["user"] = user
            recorded["role"] = role

        monkeypatch.setattr(
            "novafabric.server.rbac_store.assign_role", _assign
        )
        result = runner.invoke(
            app, ["server", "assign-role", "alice@example.com", "reader"]
        )
        assert result.exit_code == 0, result.output
        assert recorded == {"user": "alice@example.com", "role": "reader"}
        assert "assigned to" in result.output

    def test_assign_role_invalid_role_exits_1(self):
        result = runner.invoke(app, ["server", "assign-role", "bob", "superuser"])
        assert result.exit_code == 1
        assert "Invalid role" in result.output

    def test_assign_role_store_error_exits_1(self, monkeypatch):
        def _boom(user, role, assigned_by, db_path=None):
            raise RuntimeError("db locked")

        monkeypatch.setattr(
            "novafabric.server.rbac_store.assign_role", _boom
        )
        result = runner.invoke(app, ["server", "assign-role", "bob", "writer"])
        assert result.exit_code == 1
        assert "Failed to assign role" in result.output


class TestServerRevokeRole:
    def test_revoke_role_success(self, monkeypatch):
        monkeypatch.setattr(
            "novafabric.server.rbac_store.revoke_role",
            lambda user, role, db_path=None: True,
        )
        result = runner.invoke(app, ["server", "revoke-role", "alice", "writer"])
        assert result.exit_code == 0, result.output
        assert "revoked from" in result.output

    def test_revoke_role_not_found_exits_1(self, monkeypatch):
        monkeypatch.setattr(
            "novafabric.server.rbac_store.revoke_role",
            lambda user, role, db_path=None: False,
        )
        result = runner.invoke(app, ["server", "revoke-role", "alice", "writer"])
        assert result.exit_code == 1
        assert "No assignment" in result.output

    def test_revoke_role_last_admin_exits_2(self, monkeypatch):
        from novafabric.server.rbac_store import LastAdminError

        def _boom(user, role, db_path=None):
            raise LastAdminError("cannot remove last admin")

        monkeypatch.setattr(
            "novafabric.server.rbac_store.revoke_role", _boom
        )
        result = runner.invoke(app, ["server", "revoke-role", "alice", "admin"])
        assert result.exit_code == 2
        assert "Refused" in result.output

    def test_revoke_role_generic_error_exits_1(self, monkeypatch):
        def _boom(user, role, db_path=None):
            raise RuntimeError("db error")

        monkeypatch.setattr(
            "novafabric.server.rbac_store.revoke_role", _boom
        )
        result = runner.invoke(app, ["server", "revoke-role", "alice", "writer"])
        assert result.exit_code == 1
        assert "Failed to revoke role" in result.output


class TestServerFlushJwks:
    def test_flush_success(self, monkeypatch):
        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"message": "JWKS cache flushed OK"}

        import httpx

        monkeypatch.setattr(
            httpx, "post", lambda url, headers, timeout: _Resp()
        )
        result = runner.invoke(
            app, ["server", "flush-jwks-cache", "--token", "admin-tok"]
        )
        assert result.exit_code == 0, result.output
        assert "JWKS cache flushed OK" in result.output

    def test_flush_uses_stored_credentials(self, monkeypatch):
        monkeypatch.delenv("NOVA_ADMIN_TOKEN", raising=False)
        monkeypatch.setattr(
            "novafabric.cli.login.get_token", lambda url: "stored-tok"
        )

        captured = {}

        class _Resp:
            def raise_for_status(self):
                return None

            def json(self):
                return {"message": "flushed"}

        import httpx

        def _post(url, headers, timeout):
            captured["auth"] = headers.get("Authorization")
            return _Resp()

        monkeypatch.setattr(httpx, "post", _post)
        result = runner.invoke(app, ["server", "flush-jwks-cache"])
        assert result.exit_code == 0, result.output
        assert captured["auth"] == "Bearer stored-tok"

    def test_flush_http_error_exits_1(self, monkeypatch):
        import httpx

        class _ErrResp:
            status_code = 403
            text = "forbidden"

        def _post(url, headers, timeout):
            raise httpx.HTTPStatusError("err", request=None, response=_ErrResp())

        monkeypatch.setattr(httpx, "post", _post)
        result = runner.invoke(
            app, ["server", "flush-jwks-cache", "--token", "bad"]
        )
        assert result.exit_code == 1
        assert "HTTP 403" in result.output

    def test_flush_generic_error_exits_1(self, monkeypatch):
        import httpx

        def _post(url, headers, timeout):
            raise ConnectionError("refused")

        monkeypatch.setattr(httpx, "post", _post)
        result = runner.invoke(
            app, ["server", "flush-jwks-cache", "--token", "t"]
        )
        assert result.exit_code == 1
        assert "Failed:" in result.output


# ---------------------------------------------------------------------------
# nova serve — command body without binding a port (mock uvicorn.run)
# ---------------------------------------------------------------------------


class TestServe:
    def test_serve_requires_experimental(self):
        result = runner.invoke(app, ["serve"])
        assert result.exit_code == 0
        assert "EXPERIMENTAL" in result.output

    def test_serve_refuses_non_localhost_without_insecure(self):
        result = runner.invoke(
            app, ["serve", "--experimental", "--host", "0.0.0.0"]
        )
        assert result.exit_code == 2
        assert "Refusing to bind" in result.output

    def test_serve_starts_with_mocked_uvicorn(self, tmp_path, monkeypatch):
        calls = {}

        monkeypatch.setattr(
            "novafabric.serve.auth.generate_token", lambda: "tok-abc"
        )
        monkeypatch.setattr(
            "novafabric.serve.auth.write_token_file", lambda t: tmp_path / ".tok"
        )

        monkeypatch.setattr(
            "novafabric.serve.app.create_app", lambda **kw: _FakeApp()
        )
        # The "/" StaticFiles mount runs only when the static dir ships in the
        # wheel; _FakeApp.mount() accepts it without touching the filesystem.

        import uvicorn

        def _fake_run(app_obj, host, port, log_level, access_log, **kwargs):
            calls["host"] = host
            calls["port"] = port

        monkeypatch.setattr(uvicorn, "run", _fake_run)

        cap_dir = tmp_path / "capsules"  # does not exist → warning branch
        result = runner.invoke(
            app,
            [
                "serve",
                "--experimental",
                "--no-browser",
                "--capsule-dir",
                str(cap_dir),
                "--port",
                "4399",
            ],
        )
        assert result.exit_code == 0, result.output
        assert calls == {"host": "127.0.0.1", "port": 4399}
        assert "does not exist" in result.output

    def test_serve_opens_browser_and_handles_keyboardinterrupt(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "novafabric.serve.auth.generate_token", lambda: "tok-kb"
        )
        monkeypatch.setattr(
            "novafabric.serve.auth.write_token_file", lambda t: tmp_path / ".tok"
        )
        monkeypatch.setattr(
            "novafabric.serve.app.create_app", lambda **kw: _FakeApp()
        )
        # Run the browser-opener thread inline so its body is covered.
        monkeypatch.setattr(
            "novafabric.cli.serve.threading.Thread",
            lambda target, daemon=False: type(
                "T", (), {"start": staticmethod(target)}
            )(),
        )
        monkeypatch.setattr("novafabric.cli.serve.time.sleep", lambda s: None)
        opened = {}
        monkeypatch.setattr(
            "novafabric.cli.serve.webbrowser.open",
            lambda u: opened.setdefault("url", u),
        )

        import uvicorn

        def _raise_kb(*a, **k):
            raise KeyboardInterrupt

        monkeypatch.setattr(uvicorn, "run", _raise_kb)

        result = runner.invoke(
            app,
            ["serve", "--experimental", "--capsule-dir", str(tmp_path / "caps")],
        )
        assert result.exit_code == 0, result.output
        assert opened.get("url", "").startswith("http://127.0.0.1")
        assert "nova serve stopped" in result.output

    def test_serve_topology_mounts_tv5_router(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "novafabric.serve.auth.generate_token", lambda: "tok-xyz"
        )
        monkeypatch.setattr(
            "novafabric.serve.auth.write_token_file", lambda t: tmp_path / ".tok"
        )

        fake_app = _FakeApp()
        monkeypatch.setattr(
            "novafabric.serve.app.create_app", lambda **kw: fake_app
        )

        import uvicorn

        monkeypatch.setattr(
            uvicorn, "run", lambda *a, **k: None
        )

        result = runner.invoke(
            app,
            [
                "serve",
                "--experimental",
                "--no-browser",
                "--topology",
                "--capsule-dir",
                str(tmp_path / "caps"),
            ],
        )
        assert result.exit_code == 0, result.output
        # TV-5 router should have been mounted on the fake app.
        assert fake_app.included, result.output


# ---------------------------------------------------------------------------
# nova lineage-store — migrate + profile (mock OCS / migration kit)
# ---------------------------------------------------------------------------


class TestLineageMigrate:
    def test_from_ocs_missing_tenant_exits_1(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "lineage-store",
                "migrate",
                "--from-ocs",
                "--ocs-run-ids",
                "run-1",
                "--db",
                str(tmp_path / "l.db"),
            ],
        )
        assert result.exit_code == 1
        assert "--ocs-tenant is required" in result.output

    def test_from_ocs_missing_run_ids_exits_1(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "lineage-store",
                "migrate",
                "--from-ocs",
                "--ocs-tenant",
                "org",
                "--db",
                str(tmp_path / "l.db"),
            ],
        )
        assert result.exit_code == 1
        assert "--ocs-run-ids is required" in result.output

    def test_from_ocs_success_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "novafabric.lineage.migration.kit.migrate_from_ocs",
            lambda **kw: {
                "loaded": 5,
                "diverged": 0,
                "capsules_scanned": 2,
                "committed": False,
            },
        )
        result = runner.invoke(
            app,
            [
                "lineage-store",
                "migrate",
                "--from-ocs",
                "--ocs-tenant",
                "org",
                "--ocs-run-ids",
                "run-1,run-2",
                "--db",
                str(tmp_path / "l.db"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "loaded=5" in result.output
        assert "no (dry-run)" in result.output

    def test_from_ocs_divergence_exits_2(self, tmp_path, monkeypatch):
        from novafabric.lineage.migration.kit import MigrationDivergenceError

        def _boom(**kw):
            raise MigrationDivergenceError("mismatch")

        monkeypatch.setattr(
            "novafabric.lineage.migration.kit.migrate_from_ocs", _boom
        )
        result = runner.invoke(
            app,
            [
                "lineage-store",
                "migrate",
                "--from-ocs",
                "--ocs-tenant",
                "org",
                "--ocs-run-ids",
                "run-1",
                "--commit",
                "--db",
                str(tmp_path / "l.db"),
            ],
        )
        assert result.exit_code == 2
        assert "Divergence detected" in result.output

    def test_parquet_missing_arg_exits_1(self, tmp_path):
        result = runner.invoke(
            app, ["lineage-store", "migrate", "--db", str(tmp_path / "l.db")]
        )
        assert result.exit_code == 1
        assert "Provide a Parquet source" in result.output

    def test_parquet_file_not_found_exits_1(self, tmp_path):
        missing = tmp_path / "edges.parquet"
        result = runner.invoke(
            app,
            ["lineage-store", "migrate", str(missing), "--db", str(tmp_path / "l.db")],
        )
        assert result.exit_code == 1
        assert "Parquet file not found" in result.output

    def test_parquet_success_dry_run(self, tmp_path, monkeypatch):
        parquet = tmp_path / "edges.parquet"
        parquet.write_bytes(b"PAR1")

        monkeypatch.setattr(
            "novafabric.lineage.migration.kit.migrate",
            lambda **kw: {"loaded": 3, "diverged": 0, "committed": False},
        )
        result = runner.invoke(
            app,
            ["lineage-store", "migrate", str(parquet), "--db", str(tmp_path / "l.db")],
        )
        assert result.exit_code == 0, result.output
        assert "loaded=3" in result.output

    def test_parquet_divergence_exits_2(self, tmp_path, monkeypatch):
        from novafabric.lineage.migration.kit import MigrationDivergenceError

        parquet = tmp_path / "edges.parquet"
        parquet.write_bytes(b"PAR1")

        def _boom(**kw):
            raise MigrationDivergenceError("diverged")

        monkeypatch.setattr(
            "novafabric.lineage.migration.kit.migrate", _boom
        )
        result = runner.invoke(
            app,
            [
                "lineage-store",
                "migrate",
                str(parquet),
                "--commit",
                "--db",
                str(tmp_path / "l.db"),
            ],
        )
        assert result.exit_code == 2
        assert "Divergence detected" in result.output


class TestLineageProfile:
    def test_profile_unknown_target_exits_1(self):
        result = runner.invoke(
            app, ["lineage-store", "profile", "--target", "nope"]
        )
        assert result.exit_code == 1
        assert "Unknown target" in result.output

    def test_profile_kuzudb_default(self):
        result = runner.invoke(app, ["lineage-store", "profile"])
        assert result.exit_code == 0, result.output
        assert result.output.strip()

    def test_profile_janusgraph_minimal(self):
        result = runner.invoke(
            app,
            ["lineage-store", "profile", "--target", "janusgraph-minimal", "--rf", "1"],
        )
        assert result.exit_code == 0, result.output
        assert result.output.strip()

    def test_profile_janusgraph_minimal_default_tags_not_forced_to_latest(self):
        """Without --image-tag, the CLI must not force the deprecated alias —
        each image keeps its own independently-pinned default (JanusGraph
        1.1.0 in particular, not the old shared "latest").
        """
        result = runner.invoke(
            app,
            ["lineage-store", "profile", "--target", "janusgraph-minimal", "--rf", "1"],
        )
        assert result.exit_code == 0, result.output
        assert "janusgraph/janusgraph:1.1.0" in result.output
        assert "cassandra:latest" in result.output

    def test_profile_janusgraph_minimal_image_tag_alias(self):
        """--image-tag is a deprecated backward-compatible alias: it must
        still override all three images to the same tag for existing callers.
        """
        result = runner.invoke(
            app,
            [
                "lineage-store",
                "profile",
                "--target",
                "janusgraph-minimal",
                "--rf",
                "1",
                "--image-tag",
                "2.2.2",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "janusgraph/janusgraph:2.2.2" in result.output
        assert "cassandra:2.2.2" in result.output
        assert "novafabric/novafabric:2.2.2" in result.output

    def test_profile_kuzudb_vertical_image_tag_alias_still_works(self):
        """--image-tag must still work for kuzudb-vertical (single image)."""
        result = runner.invoke(
            app,
            [
                "lineage-store",
                "profile",
                "--target",
                "kuzudb-vertical",
                "--image-tag",
                "v0.9.0",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "novafabric/novafabric:v0.9.0" in result.output


# Sanity: app import + a no-op assertion to keep the import path warm.
def test_app_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "serve" in result.output
    # touch unused imports so linters don't flag them in edits
    assert json is not None
    assert pytest is not None
    assert typer is not None
