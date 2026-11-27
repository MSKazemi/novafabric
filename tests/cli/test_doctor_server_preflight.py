"""``nova doctor --check-server``: read-only server deployment preflight (#232).

One block per check, each with its success and failure cases, plus the two
properties the whole feature rests on: the preflight writes nothing, and it never
prints a secret (a token or a DSN).
"""

from __future__ import annotations

import datetime as dt
import errno
import os
import socket
from pathlib import Path

import pytest
from _help_assert import assert_flag_in_help
from typer.testing import CliRunner

from novafabric.cli import _server_preflight as pf
from novafabric.cli.main import app
from novafabric.server import schema_skew
from novafabric.server.config import OidcConfig, ServerConfig, TlsConfig

runner = CliRunner()

_IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
_needs_non_root = pytest.mark.skipif(_IS_ROOT, reason="root bypasses permission bits")


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "nf-home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(tmp_path / "audit" / "audit.jsonl"))
    return home


def _pretend_installed(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Make ``find_spec(name)`` succeed without touching any other lookup."""
    real = pf.importlib.util.find_spec
    monkeypatch.setattr(
        pf.importlib.util,
        "find_spec",
        lambda mod, *a, **k: object() if mod == name else real(mod, *a, **k),
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# server-extra
# ---------------------------------------------------------------------------


def test_server_extra_pass_when_installed() -> None:
    assert pf.check_server_extra().status == "PASS"


def test_server_extra_fail_names_missing_and_install_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = pf.importlib.util.find_spec
    monkeypatch.setattr(
        pf.importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "uvicorn" else real(name, *a, **k),
    )
    result = pf.check_server_extra()
    assert result.status == "FAIL"
    assert "uvicorn" in result.detail
    assert "novafabric[server]" in result.fix


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def test_config_default_path_absent_validates_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.server import config as server_config

    monkeypatch.setattr(server_config, "_DEFAULT_CONFIG_PATH", tmp_path / "absent.yaml")
    result, cfg = pf.check_config(None)
    assert result.status == "PASS"
    assert cfg is not None
    assert cfg.port == 7433


def test_config_explicit_missing_file_fails(tmp_path: Path) -> None:
    """`nova server start --config <typo>` silently runs on defaults; preflight must not."""
    result, cfg = pf.check_config(tmp_path / "typo.yaml")
    assert result.status == "FAIL"
    assert cfg is None
    assert "does not exist" in result.detail


def test_config_valid_file_passes(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("host: 127.0.0.1\nport: 8081\n")
    result, cfg = pf.check_config(path)
    assert result.status == "PASS"
    assert cfg is not None and cfg.port == 8081


def test_config_out_of_range_value_fails_with_key(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("port: 99999\n")
    result, cfg = pf.check_config(path)
    assert result.status == "FAIL"
    assert cfg is None
    assert "port" in result.detail


def test_config_error_does_not_echo_the_input_value(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("port: SENTINEL-VALUE-1234\n")
    result, _ = pf.check_config(path)
    assert result.status == "FAIL"
    assert "SENTINEL-VALUE-1234" not in result.detail


def test_config_invalid_yaml_fails(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("port: [unclosed\n")
    result, cfg = pf.check_config(path)
    assert result.status == "FAIL"
    assert cfg is None


@pytest.mark.parametrize(
    "broken_line",
    [
        'postgres_dsn: "postgresql://u:YAML-SECRET-PW@db/nova\n',
        "scim_token: 'YAML-SECRET-PW\n",
        "local_token: [YAML-SECRET-PW\n",
    ],
)
def test_config_yaml_error_never_quotes_the_file(tmp_path: Path, broken_line: str) -> None:
    """A YAML error's own text carries a snippet of the broken line; a secret can sit there."""
    path = tmp_path / "server.yaml"
    path.write_text("host: 127.0.0.1\n" + broken_line + "port: 7433\n")
    result, cfg = pf.check_config(path)
    assert result.status == "FAIL"
    assert cfg is None
    assert "YAML-SECRET-PW" not in result.detail + result.fix
    assert "line" in result.detail


def test_config_non_mapping_yaml_fails(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("- a\n- b\n")
    result, cfg = pf.check_config(path)
    assert result.status == "FAIL"
    assert cfg is None


def test_config_env_override_out_of_range_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("host: 127.0.0.1\n")
    monkeypatch.setenv("NOVAFABRIC_SERVER_PORT", "0")
    result, cfg = pf.check_config(path)
    assert result.status == "FAIL"
    assert cfg is None


def test_config_public_insecure_bind_fails(tmp_path: Path) -> None:
    path = tmp_path / "server.yaml"
    path.write_text("host: 0.0.0.0\ninsecure_no_auth: true\n")
    result, cfg = pf.check_config(path)
    assert result.status == "FAIL"
    assert cfg is None


# ---------------------------------------------------------------------------
# data-dir / capsule-dir
# ---------------------------------------------------------------------------


def test_directory_pass(tmp_path: Path) -> None:
    d = tmp_path / "data"
    d.mkdir(mode=0o700)
    assert pf.check_directory("data-dir", d).status == "PASS"


def test_directory_missing_but_creatable_warns_and_is_not_created(tmp_path: Path) -> None:
    d = tmp_path / "not-yet" / "data"
    result = pf.check_directory("data-dir", d)
    assert result.status == "WARN"
    assert "mkdir" in result.fix
    assert not d.exists()
    assert not d.parent.exists()


def test_directory_world_writable_fails(tmp_path: Path) -> None:
    d = tmp_path / "data"
    d.mkdir()
    d.chmod(0o777)
    result = pf.check_directory("data-dir", d)
    assert result.status == "FAIL"
    assert "world-writable" in result.detail
    assert result.fix == f"chmod o-w {d}"


def test_directory_that_is_a_file_fails(tmp_path: Path) -> None:
    f = tmp_path / "data"
    f.write_text("x")
    assert pf.check_directory("data-dir", f).status == "FAIL"


@_needs_non_root
def test_directory_not_writable_fails(tmp_path: Path) -> None:
    d = tmp_path / "data"
    d.mkdir()
    d.chmod(0o500)
    try:
        result = pf.check_directory("data-dir", d)
    finally:
        d.chmod(0o700)
    assert result.status == "FAIL"
    assert "not writable" in result.detail


@_needs_non_root
def test_directory_missing_under_unwritable_parent_fails(tmp_path: Path) -> None:
    parent = tmp_path / "ro"
    parent.mkdir()
    parent.chmod(0o500)
    try:
        result = pf.check_directory("data-dir", parent / "data")
    finally:
        parent.chmod(0o700)
    assert result.status == "FAIL"


# ---------------------------------------------------------------------------
# secret-files
# ---------------------------------------------------------------------------


def test_secret_files_skip_when_none_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    assert pf.check_secret_files(ServerConfig()).status == "SKIP"


def test_secret_files_owner_only_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    home.mkdir()
    token = home / ".server-token"
    token.write_text("t" * 43)
    token.chmod(0o600)
    result = pf.check_secret_files(ServerConfig())
    assert result.status == "PASS"
    assert "server token" in result.detail


def test_secret_files_group_readable_token_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    home.mkdir()
    token = home / ".server-token"
    token.write_text("t" * 43)
    token.chmod(0o640)
    result = pf.check_secret_files(ServerConfig())
    assert result.status == "FAIL"
    assert result.fix == f"chmod 600 {token}"
    assert "t" * 43 not in result.detail


def test_secret_files_world_readable_signing_key_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    keys = home / "keys"
    keys.mkdir(parents=True)
    key = keys / "signing_key.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\n")
    key.chmod(0o644)
    result = pf.check_secret_files(ServerConfig())
    assert result.status == "FAIL"
    assert str(key) in result.fix


def test_secret_files_missing_offline_key_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv("NOVAFABRIC_OFFLINE_KEY_PATH", str(tmp_path / "missing.pem"))
    result = pf.check_secret_files(ServerConfig())
    assert result.status == "FAIL"
    assert "NOVAFABRIC_OFFLINE_KEY_PATH" in result.detail


def test_secret_files_offline_pub_checks_private_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    pub = tmp_path / "offline.pub"
    pub.write_text("pub")
    private = tmp_path / "offline.pem"
    private.write_text("priv")
    private.chmod(0o644)
    monkeypatch.setenv("NOVAFABRIC_OFFLINE_KEY_PATH", str(pub))
    result = pf.check_secret_files(ServerConfig())
    assert result.status == "FAIL"
    assert str(private) in result.fix


def test_secret_files_offline_public_key_only_is_not_a_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    pub = tmp_path / "offline.pub"
    pub.write_text("pub")
    pub.chmod(0o644)
    monkeypatch.setenv("NOVAFABRIC_OFFLINE_KEY_PATH", str(pub))
    assert pf.check_secret_files(ServerConfig()).status == "SKIP"


# ---------------------------------------------------------------------------
# audit-log
# ---------------------------------------------------------------------------


def test_audit_log_absent_but_creatable_passes_and_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit" / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(path))
    result = pf.check_audit_log()
    assert result.status == "PASS"
    assert "NOVAFABRIC_AUDIT_LOG_PATH" in result.detail
    assert not path.parent.exists()


def test_audit_log_existing_writable_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("")
    path.chmod(0o600)
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(path))
    assert pf.check_audit_log().status == "PASS"


def test_audit_log_that_is_a_directory_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "audit.jsonl"
    path.mkdir()
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(path))
    assert pf.check_audit_log().status == "FAIL"


def test_audit_log_world_writable_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("")
    path.chmod(0o666)
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(path))
    result = pf.check_audit_log()
    assert result.status == "WARN"
    assert result.fix == f"chmod o-w {path}"


@_needs_non_root
def test_audit_log_read_only_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "audit.jsonl"
    path.write_text("")
    path.chmod(0o400)
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(path))
    result = pf.check_audit_log()
    assert result.status == "FAIL"
    assert "not writable" in result.detail


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

_SECRET_DSN = "postgresql://user:hunter2-secret@127.0.0.1:1/novafabric"


def _report(status: str, **kw: object) -> schema_skew.SkewReport:
    base: dict[str, object] = {
        "status": status,
        "backend": "postgres",
        "track": "registry",
        "db_revision": "aaa",
        "head_revision": "bbb",
        "detail": "test",
    }
    base.update(kw)
    return schema_skew.SkewReport(**base)  # type: ignore[arg-type]


def _pg_cfg(monkeypatch: pytest.MonkeyPatch, dsn: str | None = _SECRET_DSN) -> ServerConfig:
    if dsn is not None:
        monkeypatch.setenv("NOVAFABRIC_POSTGRES_DSN", dsn)
    return ServerConfig(backend="postgres")


def test_database_sqlite_missing_file_passes_and_is_not_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "sub" / "registry.db"
    result = pf.check_database(ServerConfig(db_path=str(db)))
    assert result.status == "PASS"
    assert not db.exists()
    assert not db.parent.exists()


def test_database_sqlite_foreign_stamp_fails(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "registry.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
    conn.execute("INSERT INTO alembic_version VALUES ('not-a-real-revision')")
    conn.commit()
    conn.close()
    result = pf.check_database(ServerConfig(db_path=str(db)))
    # ahead_or_foreign when the packaged head resolves; unknown on a lean install.
    assert result.status in ("FAIL", "WARN")
    if result.status == "FAIL":
        assert "refuses to start" in result.detail


def _wal_registry(path: Path, stamp: str | None) -> None:
    """A WAL-mode registry closed cleanly, so no -wal/-shm file remains (the normal state)."""
    import sqlite3

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    if stamp is not None:
        conn.execute("CREATE TABLE alembic_version (version_num TEXT)")
        conn.execute("INSERT INTO alembic_version VALUES (?)", (stamp,))
    else:
        conn.execute("CREATE TABLE assets (id TEXT)")
    conn.commit()
    conn.close()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


@pytest.mark.parametrize("stamp", ["not-a-real-revision", None])
def test_database_sqlite_wal_read_creates_no_sidecar_files(
    tmp_path: Path, stamp: str | None
) -> None:
    """mode=ro alone creates registry.db-wal/-shm (root-owned under sudo); preflight must not."""
    data = tmp_path / "data"
    data.mkdir()
    db = data / "registry.db"
    _wal_registry(db, stamp)
    before = sorted(p.name for p in data.iterdir())
    result = pf.check_database(ServerConfig(db_path=str(db)))
    assert sorted(p.name for p in data.iterdir()) == before
    assert result.status in ("FAIL", "WARN")


def test_database_sqlite_wal_read_sees_the_stamp(tmp_path: Path) -> None:
    from novafabric.migrations import registry_track

    head = registry_track.script_head("sqlite")
    if head is None:
        pytest.skip("migration scripts not resolvable in this install")
    db = tmp_path / "registry.db"
    _wal_registry(db, head)
    result = pf.check_database(ServerConfig(db_path=str(db)))
    assert result.status == "PASS"
    assert not Path(f"{db}-shm").exists()


def test_database_sqlite_tilde_path_warns_like_the_server_opens_it() -> None:
    result = pf.check_database(ServerConfig(db_path="~/nova/registry.db"))
    assert result.status == "WARN"
    assert "does not expand" in result.detail
    assert "absolute path" in result.fix


def test_database_postgres_without_dsn_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    result = pf.check_database(_pg_cfg(monkeypatch, dsn=None))
    assert result.status == "FAIL"
    assert "NOVAFABRIC_POSTGRES_DSN" in result.detail


def test_database_postgres_without_psycopg_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _pg_cfg(monkeypatch)
    real = pf.importlib.util.find_spec
    monkeypatch.setattr(
        pf.importlib.util,
        "find_spec",
        lambda name, *a, **k: None if name == "psycopg" else real(name, *a, **k),
    )
    result = pf.check_database(cfg)
    assert result.status == "FAIL"
    assert "psycopg" in result.detail


@pytest.mark.parametrize(
    ("status", "override", "expected"),
    [
        (schema_skew.STATUS_OK, False, "PASS"),
        (schema_skew.STATUS_BEHIND, False, "FAIL"),
        (schema_skew.STATUS_BEHIND, True, "WARN"),
        (schema_skew.STATUS_AHEAD, False, "FAIL"),
        (schema_skew.STATUS_AHEAD, True, "WARN"),
    ],
)
def test_database_postgres_mirrors_the_server_start_decision(
    monkeypatch: pytest.MonkeyPatch, status: str, override: bool, expected: str
) -> None:
    cfg = _pg_cfg(monkeypatch)
    if override:
        monkeypatch.setenv(schema_skew.ALLOW_SCHEMA_SKEW_ENV, "1")
    _pretend_installed(monkeypatch, "psycopg")
    monkeypatch.setattr(schema_skew, "compare_revisions", lambda *a, **k: _report(status))
    result = pf.check_database(cfg)
    assert result.status == expected
    if status == schema_skew.STATUS_BEHIND:
        assert result.fix == "nova db upgrade --track registry --backend postgres"
    assert "hunter2-secret" not in result.detail + result.fix


def test_database_postgres_unreachable_fails_without_echoing_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _pg_cfg(monkeypatch)
    _pretend_installed(monkeypatch, "psycopg")
    monkeypatch.setattr(
        schema_skew,
        "compare_revisions",
        lambda *a, **k: _report(schema_skew.STATUS_UNKNOWN, db_revision=None),
    )
    result = pf.check_database(cfg)
    assert result.status == "FAIL"
    assert "hunter2-secret" not in result.detail + result.fix
    assert "127.0.0.1" not in result.detail


def test_database_postgres_empty_schema_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _pg_cfg(monkeypatch)
    _pretend_installed(monkeypatch, "psycopg")
    monkeypatch.setattr(
        schema_skew,
        "compare_revisions",
        lambda *a, **k: _report(schema_skew.STATUS_UNSTAMPED, db_revision=None),
    )
    monkeypatch.setattr(schema_skew, "postgres_application_tables", lambda dsn: 0)
    result = pf.check_database(cfg)
    assert result.status == "FAIL"
    assert "--track registry" in result.fix


def test_database_postgres_unstamped_populated_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _pg_cfg(monkeypatch)
    _pretend_installed(monkeypatch, "psycopg")
    monkeypatch.setattr(
        schema_skew,
        "compare_revisions",
        lambda *a, **k: _report(schema_skew.STATUS_UNSTAMPED, db_revision=None),
    )
    monkeypatch.setattr(schema_skew, "postgres_application_tables", lambda dsn: 12)
    assert pf.check_database(cfg).status == "WARN"


def test_database_unknown_backend_fails() -> None:
    assert pf.check_database(ServerConfig(backend="mysql")).status == "FAIL"


# ---------------------------------------------------------------------------
# listen-port
# ---------------------------------------------------------------------------


def test_listen_port_in_use_fails() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = int(srv.getsockname()[1])
        result = pf.check_listen_port("127.0.0.1", port)
    assert result.status == "FAIL"
    assert str(port) in result.fix


def test_listen_port_free_passes() -> None:
    assert pf.check_listen_port("127.0.0.1", _free_port()).status == "PASS"


def test_listen_port_wildcard_probes_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, int]] = []

    def probe(address: str, family: socket.AddressFamily, port: int) -> int:
        seen.append((address, port))
        return errno.ECONNREFUSED

    monkeypatch.setattr(pf, "_connect_probe", probe)
    result = pf.check_listen_port("0.0.0.0", 8080)
    assert result.status == "PASS"
    assert seen == [("127.0.0.1", 8080)]
    assert "probed 127.0.0.1 only" in result.detail


def test_listen_port_never_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_bind(self: socket.socket, *a: object) -> None:
        raise AssertionError("preflight must not bind a socket")

    monkeypatch.setattr(socket.socket, "bind", no_bind)
    monkeypatch.setattr(socket.socket, "listen", no_bind)
    # A loopback port that nothing listens on: the probe connects and is refused.
    result = pf.check_listen_port("127.0.0.1", 9)
    assert result.status in ("PASS", "WARN", "FAIL")


def test_listen_port_hostname_is_not_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_resolve(*a: object, **k: object) -> None:
        raise AssertionError("preflight must not resolve host names")

    monkeypatch.setattr(socket, "getaddrinfo", no_resolve)
    result = pf.check_listen_port("nova.internal.example", 7433)
    assert result.status == "SKIP"


def test_listen_port_privileged_as_non_root_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pf, "_connect_probe", lambda *a: errno.ECONNREFUSED)
    monkeypatch.setattr(pf.os, "geteuid", lambda: 1000, raising=False)
    result = pf.check_listen_port("127.0.0.1", 443)
    assert result.status == "WARN"
    assert "CAP_NET_BIND_SERVICE" in result.detail


def test_listen_port_inconclusive_probe_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pf, "_connect_probe", lambda *a: -1)
    assert pf.check_listen_port("127.0.0.1", 8080).status == "WARN"


# ---------------------------------------------------------------------------
# tls
# ---------------------------------------------------------------------------


def _cert_pair(tmp_path: Path, *, start_days: int, end_days: int) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "preflight-test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + dt.timedelta(days=start_days))
        .not_valid_after(now + dt.timedelta(days=end_days))
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "server.crt"
    key_path = tmp_path / "server.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    return cert_path, key_path


def _tls_cfg(cert: Path | None, key: Path | None) -> ServerConfig:
    return ServerConfig(
        tls=TlsConfig(
            enabled=True,
            cert_path=str(cert) if cert else None,
            key_path=str(key) if key else None,
        )
    )


def test_tls_disabled_on_loopback_skips() -> None:
    assert pf.check_tls(ServerConfig()).status == "SKIP"


def test_tls_disabled_on_public_host_warns() -> None:
    assert pf.check_tls(ServerConfig(host="0.0.0.0")).status == "WARN"


def test_tls_valid_certificate_passes(tmp_path: Path) -> None:
    cert, key = _cert_pair(tmp_path, start_days=-1, end_days=365)
    assert pf.check_tls(_tls_cfg(cert, key)).status == "PASS"


def test_tls_expiring_soon_warns(tmp_path: Path) -> None:
    cert, key = _cert_pair(tmp_path, start_days=-1, end_days=5)
    assert pf.check_tls(_tls_cfg(cert, key)).status == "WARN"


def test_tls_expired_fails(tmp_path: Path) -> None:
    cert, key = _cert_pair(tmp_path, start_days=-30, end_days=-1)
    result = pf.check_tls(_tls_cfg(cert, key))
    assert result.status == "FAIL"
    assert "expired" in result.detail


def test_tls_not_yet_valid_fails(tmp_path: Path) -> None:
    cert, key = _cert_pair(tmp_path, start_days=3, end_days=30)
    result = pf.check_tls(_tls_cfg(cert, key))
    assert result.status == "FAIL"
    assert "not valid until" in result.detail


def test_tls_group_readable_key_fails_with_chmod(tmp_path: Path) -> None:
    cert, key = _cert_pair(tmp_path, start_days=-1, end_days=365)
    key.chmod(0o640)
    result = pf.check_tls(_tls_cfg(cert, key))
    assert result.status == "FAIL"
    assert result.fix == f"chmod 600 {key}"


def test_tls_missing_paths_fail(tmp_path: Path) -> None:
    assert pf.check_tls(_tls_cfg(None, None)).status == "FAIL"
    assert pf.check_tls(_tls_cfg(tmp_path / "a.crt", tmp_path / "a.key")).status == "FAIL"


def test_tls_non_pem_certificate_fails(tmp_path: Path) -> None:
    _, key = _cert_pair(tmp_path, start_days=-1, end_days=365)
    bogus = tmp_path / "bogus.crt"
    bogus.write_text("not a certificate")
    result = pf.check_tls(_tls_cfg(bogus, key))
    assert result.status == "FAIL"
    assert "PEM" in result.detail


# ---------------------------------------------------------------------------
# auth-token
# ---------------------------------------------------------------------------


def test_auth_token_skips_with_oidc() -> None:
    cfg = ServerConfig(oidc=OidcConfig(issuer_url="https://idp.invalid", audience="nova"))
    assert pf.check_auth_token(cfg).status == "SKIP"


def test_auth_token_insecure_no_auth_warns() -> None:
    assert pf.check_auth_token(ServerConfig(insecure_no_auth=True)).status == "WARN"


def test_auth_token_absent_passes_and_is_not_generated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _home(tmp_path, monkeypatch)
    result = pf.check_auth_token(ServerConfig())
    assert result.status == "PASS"
    assert not (home / ".server-token").exists()


def test_auth_token_short_pinned_token_warns_and_is_not_printed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOVAFABRIC_SERVER_TOKEN", "tiny-secret")
    result = pf.check_auth_token(ServerConfig())
    assert result.status == "WARN"
    assert "11 characters" in result.detail
    assert "tiny-secret" not in result.detail + result.fix


def test_auth_token_long_pinned_token_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "x" * pf.GENERATED_TOKEN_CHARS
    monkeypatch.setenv("NOVAFABRIC_SERVER_TOKEN", secret)
    result = pf.check_auth_token(ServerConfig())
    assert result.status == "PASS"
    assert secret not in result.detail


def test_auth_token_empty_file_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = _home(tmp_path, monkeypatch)
    home.mkdir()
    (home / ".server-token").write_text("\n")
    result = pf.check_auth_token(ServerConfig())
    assert result.status == "FAIL"
    assert "empty" in result.detail


def test_generated_token_length_matches_the_server_generator() -> None:
    """The WARN threshold is the server's own token, not an invented policy."""
    import secrets

    assert len(secrets.token_urlsafe(32)) == pf.GENERATED_TOKEN_CHARS


# ---------------------------------------------------------------------------
# seal-key
# ---------------------------------------------------------------------------


def test_seal_skips_when_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    assert pf.check_seal_key().status == "SKIP"


def test_seal_config_env_pointing_at_missing_file_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(tmp_path / "missing.yaml"))
    assert pf.check_seal_key().status == "FAIL"


def test_seal_local_key_owner_only_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cert, key = _cert_pair(tmp_path, start_days=-1, end_days=365)
    cfg = tmp_path / "novaseal.yaml"
    cfg.write_text(f"profile: local\nkey_path: {key}\ncert_path: {cert}\n")
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(cfg))
    assert pf.check_seal_key().status == "PASS"


def test_seal_local_key_group_readable_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert, key = _cert_pair(tmp_path, start_days=-1, end_days=365)
    key.chmod(0o644)
    cfg = tmp_path / "novaseal.yaml"
    cfg.write_text(f"profile: local\nkey_path: {key}\ncert_path: {cert}\n")
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(cfg))
    result = pf.check_seal_key()
    assert result.status == "FAIL"
    assert result.fix == f"chmod 600 {key}"


def test_seal_local_key_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cert, _ = _cert_pair(tmp_path, start_days=-1, end_days=365)
    cfg = tmp_path / "novaseal.yaml"
    cfg.write_text(f"profile: local\nkey_path: {tmp_path / 'gone.pem'}\ncert_path: {cert}\n")
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(cfg))
    assert pf.check_seal_key().status == "FAIL"


def test_seal_kms_profile_passes_without_contacting_kms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cert, _ = _cert_pair(tmp_path, start_days=-1, end_days=365)
    cfg = tmp_path / "novaseal.yaml"
    cfg.write_text(
        "profile: aws_kms\n"
        "kms_key_id: arn:aws:kms:us-east-1:000000000000:key/test\n"
        f"cert_path: {cert}\n"
    )
    monkeypatch.setenv("NOVAFABRIC_SEAL_CONFIG", str(cfg))

    def no_network(*a: object, **k: object) -> None:
        raise AssertionError("preflight must not contact a KMS")

    monkeypatch.setattr(socket, "create_connection", no_network)
    result = pf.check_seal_key()
    assert result.status == "PASS"
    assert "aws_kms" in result.detail


# ---------------------------------------------------------------------------
# run_server_preflight: order, SKIP cascade, writes nothing
# ---------------------------------------------------------------------------

_ALL_CHECKS = [
    "server-extra",
    "config",
    "data-dir",
    "capsule-dir",
    "secret-files",
    "database",
    "listen-port",
    "tls",
    "auth-token",
    "audit-log",
    "seal-key",
]


def _snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_run_reports_every_check_in_order_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv("NOVAFABRIC_SERVER_PORT", str(_free_port()))
    config = tmp_path / "server.yaml"
    config.write_text("host: 127.0.0.1\n")
    before = _snapshot(tmp_path)
    results = pf.run_server_preflight(config)
    assert [r.name for r in results] == _ALL_CHECKS
    assert _snapshot(tmp_path) == before
    assert not any(r.status == "FAIL" for r in results), results


def test_run_skips_config_dependent_checks_when_config_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)
    results = {r.name: r for r in pf.run_server_preflight(tmp_path / "absent.yaml")}
    assert results["config"].status == "FAIL"
    for name in ("database", "listen-port", "tls", "auth-token"):
        assert results[name].status == "SKIP"


def test_run_reports_a_crashing_check_as_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _home(tmp_path, monkeypatch)

    def boom() -> pf.PreflightResult:
        raise RuntimeError("bug")

    monkeypatch.setattr(pf, "check_seal_key", boom)
    results = {r.name: r for r in pf.run_server_preflight(tmp_path / "absent.yaml")}
    assert results["seal-key"].status == "FAIL"
    assert "RuntimeError" in results["seal-key"].detail


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_help_lists_the_preflight_flags() -> None:
    result = runner.invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--check-server")
    assert_flag_in_help(result, "--server-config")


def test_cli_clean_environment_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv("NOVAFABRIC_SERVER_PORT", str(_free_port()))
    config = tmp_path / "server.yaml"
    config.write_text("host: 127.0.0.1\n")
    result = runner.invoke(app, ["doctor", "--check-server", "--server-config", str(config)])
    assert isinstance(result.exception, SystemExit) or result.exception is None
    assert result.exit_code == 0, result.output
    assert "Server deployment preflight" in result.output
    assert "0 fail" in result.output


def test_cli_any_fail_exits_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _home(tmp_path, monkeypatch)
    result = runner.invoke(
        app, ["doctor", "--check-server", "--server-config", str(tmp_path / "typo.yaml")]
    )
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "FAIL" in result.output


def test_cli_never_prints_the_token_or_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from novafabric.server import config as server_config

    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(server_config, "_DEFAULT_CONFIG_PATH", tmp_path / "absent.yaml")
    _pretend_installed(monkeypatch, "psycopg")
    monkeypatch.setenv("NOVAFABRIC_SERVER_TOKEN", "pinned-secret-token-value")
    monkeypatch.setenv("NOVAFABRIC_POSTGRES_DSN", _SECRET_DSN)
    monkeypatch.setenv("NOVAFABRIC_SERVER_BACKEND", "postgres")
    monkeypatch.setattr(
        schema_skew,
        "compare_revisions",
        lambda *a, **k: _report(schema_skew.STATUS_UNKNOWN, db_revision=None),
    )
    result = runner.invoke(app, ["doctor", "--check-server"])
    assert "pinned-secret-token-value" not in result.output
    assert "hunter2-secret" not in result.output
