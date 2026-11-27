"""Read-only preflight checks for a ``nova server start`` deployment (#232).

``nova doctor --check-server`` runs these before an operator starts the server, so a
misconfiguration surfaces as one line with a fix instead of a failed start, a 401 storm
or a server that starts and then cannot write. Each check returns one
:class:`PreflightResult` with a status of ``PASS``, ``FAIL``, ``WARN`` or ``SKIP`` and,
for anything but ``PASS``/``SKIP``, a one-line fix.

The checks are strictly read-only. They:

* never create, chmod, write or delete a file or directory (no ``mkdir``, no token
  generation, no schema bootstrap — the server's own ``get_connection`` creates the
  registry directory, so it is deliberately not used here);
* never bind a socket. The listen-port check makes one TCP ``connect`` to the
  configured address (loopback for a wildcard bind) and closes it;
* never resolve a host name, contact a cloud KMS, a timestamp authority or any other
  external service. The only connection off the local host is the one the operator
  configured: with the ``postgres`` backend, at most two short connections to
  ``NOVAFABRIC_POSTGRES_DSN`` (2 s connect timeout each) that run ``SELECT``
  statements only;
* never print a secret. A token is measured, never shown; a DSN is never echoed.

They read the same configuration ``nova server start`` reads: the YAML file
(``--server-config``, default ``~/.config/novafabric/server.yaml``) through the server's
own :func:`novafabric.server.config.load_config`, plus the ``NOVAFABRIC_SERVER_*`` /
``NOVAFABRIC_*`` environment. CLI flags of ``nova server start`` (``--host``, ``--port``,
``--backend``) are not seen; put them in the file or environment to check them.
"""

from __future__ import annotations

import errno
import importlib.util
import ipaddress
import os
import socket
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from novafabric.server import schema_skew
    from novafabric.server.config import ServerConfig

Status = Literal["PASS", "FAIL", "WARN", "SKIP"]

#: Characters in the token the server generates itself (``secrets.token_urlsafe(32)``,
#: :func:`novafabric.server.local_token.generate_token`). The server enforces no
#: minimum on a pinned token; preflight warns below this length rather than failing.
GENERATED_TOKEN_CHARS = 43

#: A TLS certificate expiring within this window is a WARN, not a PASS.
CERT_EXPIRY_WARN_DAYS = 30

#: Bound on the listen-port probe; a loopback connect answers in microseconds.
PORT_PROBE_TIMEOUT_S = 0.5

_SECRET_MODE_MASK = 0o077  # group/world bits: the server's own owner-only posture


@dataclass(frozen=True)
class PreflightResult:
    """One check's outcome. ``fix`` is a single line; empty for PASS/SKIP."""

    name: str
    status: Status
    detail: str
    fix: str = ""


def _result(name: str, status: Status, detail: str, fix: str = "") -> PreflightResult:
    return PreflightResult(name=name, status=status, detail=detail, fix=fix)


def _mode(st: os.stat_result) -> str:
    return oct(stat.S_IMODE(st.st_mode))


def _describe_load_error(exc: BaseException) -> str:
    """Name a config load error without quoting the file.

    A YAML error's text carries a snippet of the offending line, and a broken line
    may hold a secret (an unclosed quote after ``postgres_dsn:`` echoes the DSN).
    Only the error type and, for YAML, its line and column are reported.
    """
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    line = getattr(mark, "line", None)
    column = getattr(mark, "column", None)
    if isinstance(line, int) and isinstance(column, int):
        return f"{type(exc).__name__} at line {line + 1}, column {column + 1}"
    if isinstance(exc, OSError) and exc.strerror:
        return f"{type(exc).__name__}: {exc.strerror}"
    return type(exc).__name__


def _nearest_existing_ancestor(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        try:
            if candidate.exists():
                return candidate
        except OSError:
            return None
    return None


# ---------------------------------------------------------------------------
# Server extra + config
# ---------------------------------------------------------------------------


def check_server_extra() -> PreflightResult:
    """The distributions ``nova server start`` imports are installed (no import run)."""
    missing = [name for name in ("fastapi", "uvicorn") if importlib.util.find_spec(name) is None]
    if missing:
        return _result(
            "server-extra",
            "FAIL",
            f"missing: {', '.join(missing)}",
            "pip install 'novafabric[server]'",
        )
    return _result("server-extra", "PASS", "fastapi and uvicorn are installed")


def check_config(config_path: Path | None) -> tuple[PreflightResult, ServerConfig | None]:
    """Parse and validate the config with the server's real loader.

    An explicitly named file that does not exist is a FAIL: ``nova server start
    --config <missing>`` silently starts on built-in defaults, which is exactly the
    mistake a preflight should catch.
    """
    try:
        import pydantic

        from novafabric.server import config as server_config
    except ImportError as exc:
        return (
            _result(
                "config",
                "FAIL",
                f"server config module unavailable: {exc}",
                "pip install 'novafabric[server]'",
            ),
            None,
        )

    path = config_path if config_path is not None else server_config._DEFAULT_CONFIG_PATH
    if config_path is not None and not config_path.is_file():
        return (
            _result(
                "config",
                "FAIL",
                f"{config_path} does not exist or is not a file (nova server start "
                "would silently use built-in defaults)",
                "fix the --config path, or create the file",
            ),
            None,
        )

    try:
        cfg = server_config.load_config(path)
    except pydantic.ValidationError as exc:
        # loc + msg only: an input value could be anything the operator typed.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        return (
            _result(
                "config",
                "FAIL",
                f"{path}: invalid ({problems})",
                "correct the named keys (or the NOVAFABRIC_SERVER_* variable that set them)",
            ),
            None,
        )
    except Exception as exc:  # noqa: BLE001 — YAML errors, a non-mapping file, OSError
        return (
            _result(
                "config",
                "FAIL",
                f"{path}: could not be loaded ({_describe_load_error(exc)})",
                "make the file a YAML mapping of server keys (see docs/ops/server-deployment.md)",
            ),
            None,
        )

    if path.is_file():
        detail = f"{path} parses and validates"
    else:
        detail = f"no file at {path}; built-in defaults plus environment validate"
    return _result("config", "PASS", detail), cfg


# ---------------------------------------------------------------------------
# Filesystem
# ---------------------------------------------------------------------------


def check_directory(name: str, path: Path) -> PreflightResult:
    """*path* exists (or can be created), is writable by this user, not world-writable."""
    try:
        st = path.stat()
    except FileNotFoundError:
        ancestor = _nearest_existing_ancestor(path.parent)
        if ancestor is not None and os.access(ancestor, os.W_OK | os.X_OK):
            return _result(
                name,
                "WARN",
                f"{path} does not exist yet; it is created on first use",
                f"mkdir -p -m 700 {path}",
            )
        return _result(
            name,
            "FAIL",
            f"{path} does not exist and {ancestor or 'its parent'} is not writable",
            f"create {path} as the service user (mkdir -p -m 700), or set NOVAFABRIC_HOME",
        )
    except OSError as exc:
        return _result(name, "FAIL", f"{path}: cannot stat ({exc.strerror})", f"check {path}")

    if not stat.S_ISDIR(st.st_mode):
        return _result(name, "FAIL", f"{path} exists but is not a directory", f"move {path} away")
    if st.st_mode & stat.S_IWOTH:
        return _result(
            name,
            "FAIL",
            f"{path} is world-writable (mode {_mode(st)})",
            f"chmod o-w {path}",
        )
    if not os.access(path, os.W_OK | os.X_OK):
        return _result(
            name,
            "FAIL",
            f"{path} is not writable by the current user (mode {_mode(st)})",
            f"chown the directory to the service user, or chmod u+rwx {path}",
        )
    return _result(name, "PASS", f"{path} exists and is writable (mode {_mode(st)})")


def _offline_private_key(offline_key_path: str) -> tuple[Path | None, Path]:
    """``(private key to check or None, configured path)`` for NOVAFABRIC_OFFLINE_KEY_PATH."""
    configured = Path(offline_key_path).expanduser()
    try:
        from novafabric.server.offline_tokens import PUBLIC_KEY_SUFFIX, signing_key_path
    except ImportError:
        # Lean install: treat the configured path itself as the key.
        return (None if configured.suffix == ".pub" else configured), configured
    if configured.suffix != PUBLIC_KEY_SUFFIX:
        return configured, configured
    private = signing_key_path(configured)
    return (private if private.is_file() else None), configured


def check_secret_files(cfg: ServerConfig | None) -> PreflightResult:
    """Secret files the server reads are owner-only (no group/world bits)."""
    from novafabric import _paths

    candidates: list[tuple[str, Path]] = [
        ("server token", _paths.server_token_path()),
        ("evidence signing key", _paths.local_key_path()),
    ]
    missing_configured: list[str] = []
    if cfg is not None and cfg.offline_key_path:
        private, configured = _offline_private_key(cfg.offline_key_path)
        if not configured.exists():
            missing_configured.append(f"NOVAFABRIC_OFFLINE_KEY_PATH {configured}")
        if private is not None:
            candidates.append(("offline signing key", private))

    exposed: list[str] = []
    exposed_paths: list[str] = []
    unreadable: list[str] = []
    checked: list[str] = []
    for label, path in candidates:
        try:
            st = path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            unreadable.append(f"{label} {path} ({exc.strerror})")
            continue
        checked.append(label)
        if st.st_mode & _SECRET_MODE_MASK:
            exposed.append(f"{label} {path} (mode {_mode(st)})")
            exposed_paths.append(str(path))

    if missing_configured:
        return _result(
            "secret-files",
            "FAIL",
            "configured but missing: " + ", ".join(missing_configured),
            "point NOVAFABRIC_OFFLINE_KEY_PATH at the existing key (or its .pub)",
        )
    if exposed:
        return _result(
            "secret-files",
            "FAIL",
            "group/world-accessible: " + "; ".join(exposed),
            "chmod 600 " + " ".join(exposed_paths),
        )
    if unreadable:
        return _result(
            "secret-files",
            "FAIL",
            "cannot stat: " + "; ".join(unreadable),
            "run preflight as the service user",
        )
    if not checked:
        return _result("secret-files", "SKIP", "no secret files present yet")
    return _result("secret-files", "PASS", "owner-only: " + ", ".join(checked))


def check_audit_log() -> PreflightResult:
    """The hash-chained audit log is (or can be created) writable."""
    from novafabric.audit._paths import AUDIT_LOG_PATH_ENV, resolve_audit_log_path

    path = resolve_audit_log_path()
    source = (
        AUDIT_LOG_PATH_ENV if os.environ.get(AUDIT_LOG_PATH_ENV, "").strip() else "default location"
    )
    try:
        st = path.stat()
    except FileNotFoundError:
        ancestor = _nearest_existing_ancestor(path.parent)
        if ancestor is not None and os.access(ancestor, os.W_OK | os.X_OK):
            return _result("audit-log", "PASS", f"{path} ({source}) will be created on first write")
        return _result(
            "audit-log",
            "FAIL",
            f"{path} ({source}) does not exist and {ancestor or 'its parent'} is not writable",
            f"set {AUDIT_LOG_PATH_ENV} to a path the service user can write",
        )
    except OSError as exc:
        return _result(
            "audit-log", "FAIL", f"{path}: cannot stat ({exc.strerror})", f"check {path}"
        )

    if not stat.S_ISREG(st.st_mode):
        return _result(
            "audit-log", "FAIL", f"{path} exists but is not a regular file", f"move {path} away"
        )
    if not os.access(path, os.W_OK):
        return _result(
            "audit-log",
            "FAIL",
            f"{path} ({source}) is not writable by the current user",
            f"chown {path} to the service user, or set {AUDIT_LOG_PATH_ENV}",
        )
    if st.st_mode & stat.S_IWOTH:
        return _result(
            "audit-log",
            "WARN",
            f"{path} is world-writable (mode {_mode(st)}); any local user can append",
            f"chmod o-w {path}",
        )
    return _result("audit-log", "PASS", f"{path} ({source}) is writable")


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def check_database(cfg: ServerConfig) -> PreflightResult:
    """The configured backend is reachable and its registry track is at head.

    Mirrors the server's own start-up decision table (ADR-0211, ``schema_skew``):
    states the server refuses to start on are FAIL, states it starts on with a
    warning are WARN. Uses the same read-only comparator; never migrates or stamps.
    """
    from novafabric.server import schema_skew

    backend = cfg.backend
    override = schema_skew.allow_schema_skew()
    upgrade = f"nova db upgrade --track registry --backend {backend}"

    if backend == "postgres":
        if not cfg.postgres_dsn:
            return _result(
                "database",
                "FAIL",
                "backend is postgres but NOVAFABRIC_POSTGRES_DSN is not set",
                "export NOVAFABRIC_POSTGRES_DSN (environment only; server.yaml ignores it)",
            )
        if importlib.util.find_spec("psycopg") is None:
            return _result(
                "database",
                "FAIL",
                "backend is postgres but psycopg is not installed",
                "pip install 'novafabric[server]'",
            )
        target: str | Path = cfg.postgres_dsn
    elif backend == "sqlite":
        from novafabric import _paths

        # The exact path the server opens (server/app.py: Path(config.db_path),
        # unexpanded), so a literal "~" is reported rather than silently fixed here.
        target = Path(cfg.db_path) if cfg.db_path else _paths.registry_db_path()
        if str(target).startswith("~"):
            return _result(
                "database",
                "WARN",
                f"sqlite: db_path {target} starts with '~', which the server does not "
                "expand; it would open a relative path under its working directory",
                "use an absolute path in db_path / NOVAFABRIC_SERVER_DB_PATH",
            )
        if not Path(target).exists():
            return _result(
                "database",
                "PASS",
                f"sqlite: {target} does not exist yet; the server creates it at first start",
            )
    else:
        return _result(
            "database",
            "FAIL",
            f"unknown backend {backend!r}",
            "set backend to 'sqlite' or 'postgres'",
        )

    if backend == "sqlite":
        report = _compare_sqlite_read_only(Path(target))
    else:
        report = schema_skew.compare_revisions(backend, target)
    where = backend if backend == "postgres" else f"sqlite {target}"
    revs = f"db={report.db_revision or '-'} head={report.head_revision or '-'}"

    if report.status == schema_skew.STATUS_OK:
        return _result("database", "PASS", f"{where}: registry track at head ({revs})")

    if report.status in (schema_skew.STATUS_BEHIND, schema_skew.STATUS_AHEAD):
        what = (
            "behind the code (migrations pending)"
            if report.status == schema_skew.STATUS_BEHIND
            else "ahead of / unknown to this build"
        )
        fix = upgrade if report.status == schema_skew.STATUS_BEHIND else report.remediation
        if override:
            return _result(
                "database",
                "WARN",
                f"{where}: schema {what} ({revs}); start allowed by "
                f"{schema_skew.ALLOW_SCHEMA_SKEW_ENV}",
                fix,
            )
        return _result(
            "database", "FAIL", f"{where}: schema {what} ({revs}); the server refuses to start", fix
        )

    if report.status == schema_skew.STATUS_UNSTAMPED:
        if backend == "postgres":
            tables = schema_skew.postgres_application_tables(str(target))
            if tables == 0:
                status: Status = "WARN" if override else "FAIL"
                return _result(
                    "database",
                    status,
                    "postgres: reachable but has no tables (migration never ran); "
                    + (
                        f"start allowed by {schema_skew.ALLOW_SCHEMA_SKEW_ENV}"
                        if override
                        else "the server refuses to start"
                    ),
                    upgrade,
                )
        return _result(
            "database",
            "WARN",
            f"{where}: reachable but not alembic-stamped; the server starts with a warning",
            "nova doctor --check-storage",
        )

    # STATUS_UNKNOWN
    if report.head_revision is None and report.db_revision is not None:
        return _result(
            "database",
            "WARN",
            f"{where}: migration script head unresolvable in this install ({report.detail})",
            "install the full package (the migration scripts ship with it)",
        )
    if backend == "postgres":
        return _result(
            "database",
            "FAIL",
            "postgres: could not connect or read alembic_version within the 2 s timeout "
            "(DSN not shown)",
            "check host, port, credentials and pg_hba.conf for NOVAFABRIC_POSTGRES_DSN",
        )
    return _result(
        "database",
        "WARN",
        f"{where}: revision not determinable ({report.detail})",
        "nova doctor --check-storage",
    )


def _sqlite_ro_uri(path: Path) -> str:
    """A read-only SQLite URI for *path* that creates no ``-wal`` / ``-shm`` file.

    The registry is always WAL. ``mode=ro`` alone still creates both sidecars when
    they are absent (and under ``sudo`` they would be root-owned, locking the service
    user out). When neither sidecar exists the last writer closed cleanly and
    checkpointed, so ``immutable=1`` reads the complete database without them. When
    they exist, nothing is created and plain ``mode=ro`` sees uncheckpointed pages.
    """
    sidecars = (Path(f"{path}-wal"), Path(f"{path}-shm"))
    query = "mode=ro" if any(p.exists() for p in sidecars) else "mode=ro&immutable=1"
    return f"{path.resolve().as_uri()}?{query}"


def _compare_sqlite_read_only(path: Path) -> schema_skew.SkewReport:
    """``schema_skew.compare_revisions("sqlite", path)`` without touching the directory.

    Same five states and the same packaged script head; only the stamp read differs
    (:func:`_sqlite_ro_uri`). The server's own start-up comparator is unchanged.
    """
    import sqlite3

    from novafabric.migrations import registry_track
    from novafabric.server import schema_skew

    def report(
        status: schema_skew.SkewStatus, stamp: str | None, head: str | None, detail: str
    ) -> schema_skew.SkewReport:
        return schema_skew.SkewReport(
            status=status,
            backend="sqlite",
            track="registry",
            db_revision=stamp,
            head_revision=head,
            detail=detail,
        )

    head = registry_track.script_head("sqlite")
    try:
        conn = sqlite3.connect(_sqlite_ro_uri(path), uri=True, timeout=2)
        try:
            row = conn.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return report(schema_skew.STATUS_UNSTAMPED, None, head, "no alembic_version stamp")
        return report(schema_skew.STATUS_UNKNOWN, None, head, "database unreadable")
    except sqlite3.Error:
        return report(schema_skew.STATUS_UNKNOWN, None, head, "database unreadable")
    if row is None or not row[0]:
        return report(schema_skew.STATUS_UNSTAMPED, None, head, "no alembic_version stamp")
    stamp = str(row[0])
    if head is None:
        return report(schema_skew.STATUS_UNKNOWN, stamp, None, "migration script head unresolvable")
    if stamp == head:
        return report(schema_skew.STATUS_OK, stamp, head, "database stamp matches the script head")
    ancestry = registry_track.head_ancestry("sqlite")
    if ancestry is not None and stamp in ancestry:
        return report(schema_skew.STATUS_BEHIND, stamp, head, "stamp is an ancestor of head")
    return report(schema_skew.STATUS_AHEAD, stamp, head, "stamp is not known to this build")


# ---------------------------------------------------------------------------
# Network listener
# ---------------------------------------------------------------------------


def _probe_address(host: str) -> tuple[str, socket.AddressFamily] | None:
    """The address to probe for *host*, or None for a name (never resolved here)."""
    if host in ("", "0.0.0.0", "localhost"):
        return "127.0.0.1", socket.AF_INET
    if host == "::":
        return "::1", socket.AF_INET6
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    return str(ip), family


def _connect_probe(address: str, family: socket.AddressFamily, port: int) -> int:
    """``connect_ex`` result for one TCP connect to *address*:*port*; -1 on timeout.

    The only socket operation preflight performs: no ``bind``, no ``listen``.
    """
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.settimeout(PORT_PROBE_TIMEOUT_S)
        return sock.connect_ex((address, port))
    except OSError as exc:
        return exc.errno or -1
    finally:
        sock.close()


def check_listen_port(host: str, port: int) -> PreflightResult:
    """Nothing already listens on the configured address. Connects; never binds."""
    probe = _probe_address(host)
    if probe is None:
        return _result(
            "listen-port", "SKIP", f"host {host!r} is a name; preflight does not resolve names"
        )
    address, family = probe
    try:
        code = _connect_probe(address, family, port)
    except OSError as exc:  # socket() itself failed, e.g. no IPv6 on this host
        return _result(
            "listen-port",
            "WARN",
            f"cannot probe {address}:{port} ({exc.strerror})",
            f"check manually: ss -ltn 'sport = :{port}'",
        )

    if code == 0:
        return _result(
            "listen-port",
            "FAIL",
            f"{address}:{port} already accepts connections (another process holds the port)",
            f"stop that process (ss -ltnp 'sport = :{port}') or choose another port",
        )
    if code != errno.ECONNREFUSED:
        return _result(
            "listen-port",
            "WARN",
            f"{address}:{port} probe inconclusive ({os.strerror(code) if code > 0 else 'timeout'})",
            f"check manually: ss -ltn 'sport = :{port}'",
        )
    geteuid = getattr(os, "geteuid", None)
    if port < 1024 and geteuid is not None and geteuid() != 0:
        return _result(
            "listen-port",
            "WARN",
            f"{host}:{port} is free but privileged; binding it needs root or CAP_NET_BIND_SERVICE",
            "use a port >= 1024 behind a reverse proxy",
        )
    scope = (
        ""
        if address == host
        else f" (probed {address} only; a listener on another interface is not seen)"
    )
    return _result("listen-port", "PASS", f"{host}:{port} is free{scope}")


def check_tls(cfg: ServerConfig) -> PreflightResult:
    """TLS material is usable and the certificate is in its validity window."""
    from novafabric.server.config import TlsConfigError, _is_loopback_host, check_tls_config

    tls = cfg.tls
    if not tls.enabled:
        if _is_loopback_host(cfg.host):
            return _result("tls", "SKIP", "tls.enabled is false (loopback listener)")
        return _result(
            "tls",
            "WARN",
            f"tls.enabled is false and host {cfg.host} is not loopback: plaintext HTTP "
            "unless a reverse proxy terminates TLS",
            "terminate TLS at a proxy, or set tls.enabled with cert_path/key_path",
        )
    try:
        check_tls_config(tls)
    except TlsConfigError as exc:
        fix = (
            f"chmod 600 {tls.key_path}"
            if "group/world" in str(exc)
            else "fix the tls block (NOVA_TLS_CERT_PATH / NOVA_TLS_KEY_PATH)"
        )
        return _result("tls", "FAIL", str(exc), fix)

    cert_path = Path(str(tls.cert_path))
    key_path = Path(str(tls.key_path))
    for label, path in (("certificate", cert_path), ("private key", key_path)):
        if not os.access(path, os.R_OK):
            return _result(
                "tls",
                "FAIL",
                f"TLS {label} {path} is not readable by the current user",
                f"chown {path} to the service user",
            )

    try:
        from cryptography import x509

        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception as exc:  # noqa: BLE001 — unreadable or not PEM
        return _result(
            "tls",
            "FAIL",
            f"{cert_path} is not a PEM certificate ({type(exc).__name__})",
            "point tls.cert_path at a PEM-encoded certificate",
        )

    now = datetime.now(timezone.utc)
    not_before = cert.not_valid_before_utc
    not_after = cert.not_valid_after_utc
    if now < not_before:
        return _result(
            "tls",
            "FAIL",
            f"certificate not valid until {not_before:%Y-%m-%d %H:%M} UTC",
            "check the host clock, or install a certificate valid now",
        )
    if now >= not_after:
        return _result(
            "tls",
            "FAIL",
            f"certificate expired {not_after:%Y-%m-%d} UTC",
            "renew the certificate",
        )
    if not_after - now < timedelta(days=CERT_EXPIRY_WARN_DAYS):
        return _result(
            "tls",
            "WARN",
            f"certificate expires {not_after:%Y-%m-%d} UTC (within {CERT_EXPIRY_WARN_DAYS} days)",
            "renew the certificate",
        )
    return _result("tls", "PASS", f"certificate and key usable; expires {not_after:%Y-%m-%d} UTC")


# ---------------------------------------------------------------------------
# Auth token + seal key
# ---------------------------------------------------------------------------


def check_auth_token(cfg: ServerConfig) -> PreflightResult:
    """The local bearer token (ADR-0184) is usable and no weaker than a generated one.

    The token is read to measure it; it is never printed.
    """
    from novafabric import _paths
    from novafabric.server.local_token import TOKEN_ENV

    if cfg.oidc.enabled:
        return _result("auth-token", "SKIP", "OIDC is enabled; the local token is not used")
    if cfg.insecure_no_auth:
        return _result(
            "auth-token",
            "WARN",
            "insecure_no_auth is set: every request is anonymous admin",
            "unset insecure_no_auth / NOVAFABRIC_SERVER_INSECURE_NO_AUTH",
        )

    env_token = os.environ.get(TOKEN_ENV, "").strip()
    token_file = _paths.server_token_path()
    if env_token:
        token, source = env_token, TOKEN_ENV
    elif token_file.exists():
        try:
            token = token_file.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            return _result(
                "auth-token",
                "FAIL",
                f"{token_file} is not readable ({type(exc).__name__})",
                f"chown {token_file} to the service user",
            )
        source = str(token_file)
        if not token:
            return _result(
                "auth-token",
                "FAIL",
                f"{token_file} is empty: the server would use a new, unsaved token on every start",
                f"remove {token_file} so the server writes a fresh one, or pin {TOKEN_ENV}",
            )
    else:
        return _result(
            "auth-token",
            "PASS",
            f"no token yet; the server generates a 32-byte random token at first start "
            f"and stores it at {token_file} (mode 0600)",
        )

    if len(token) < GENERATED_TOKEN_CHARS:
        return _result(
            "auth-token",
            "WARN",
            f"token from {source} is {len(token)} characters, shorter than the "
            f"{GENERATED_TOKEN_CHARS} the server generates (the server enforces no minimum)",
            "pin a token from: python -c 'import secrets; print(secrets.token_urlsafe(32))'",
        )
    return _result("auth-token", "PASS", f"token from {source} is {len(token)} characters")


def check_seal_key() -> PreflightResult:
    """When NovaSeal is configured, its signing key and certificate are present."""
    try:
        from novafabric.trust.novaseal.config import SealConfigError, load_signing_profile
    except ImportError as exc:
        return _result("seal-key", "SKIP", f"NovaSeal unavailable in this install ({exc})")

    try:
        profile = load_signing_profile()
    except SealConfigError as exc:
        return _result("seal-key", "FAIL", str(exc), "fix novaseal.yaml (NOVAFABRIC_SEAL_CONFIG)")
    except Exception as exc:  # noqa: BLE001 — YAML or OS error reading the profile
        return _result(
            "seal-key",
            "FAIL",
            f"novaseal.yaml could not be read ({_describe_load_error(exc)})",
            "fix novaseal.yaml (NOVAFABRIC_SEAL_CONFIG)",
        )
    if profile is None:
        return _result(
            "seal-key",
            "SKIP",
            "NovaSeal not configured (no NOVAFABRIC_SEAL_CONFIG, no novaseal.yaml in "
            "NOVAFABRIC_HOME)",
        )

    cert = profile.cert_path
    if cert is not None and not os.access(cert, os.R_OK):
        return _result(
            "seal-key",
            "FAIL",
            f"seal certificate {cert} is missing or not readable",
            f"chown {cert} to the service user",
        )

    if profile.profile != "local":
        return _result(
            "seal-key",
            "PASS",
            f"profile {profile.profile}: key held by the KMS (not contacted; preflight "
            "makes no cloud call)",
        )

    key = profile.key_path
    if key is None or not os.access(key, os.R_OK):
        return _result(
            "seal-key",
            "FAIL",
            f"seal key {key} is missing or not readable",
            "point key_path at the ECDSA P-256 key, readable by the service user",
        )
    try:
        st = key.stat()
    except OSError as exc:
        return _result("seal-key", "FAIL", f"seal key {key}: cannot stat ({exc.strerror})")
    if st.st_mode & _SECRET_MODE_MASK:
        return _result(
            "seal-key",
            "FAIL",
            f"seal key {key} is group/world-accessible (mode {_mode(st)})",
            f"chmod 600 {key}",
        )
    return _result("seal-key", "PASS", f"local profile: key {key} present and owner-only")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_server_preflight(config_path: Path | None = None) -> list[PreflightResult]:
    """Run every check in order. Never raises for a check's own failure."""
    from novafabric import _paths

    results: list[PreflightResult] = [check_server_extra()]
    config_result, cfg = check_config(config_path)
    results.append(config_result)

    results.append(_guarded("data-dir", lambda: check_directory("data-dir", _paths.nova_home())))
    results.append(
        _guarded(
            "capsule-dir", lambda: check_directory("capsule-dir", _paths.default_capsule_dir())
        )
    )
    results.append(_guarded("secret-files", lambda: check_secret_files(cfg)))

    if cfg is None:
        for name in ("database", "listen-port", "tls", "auth-token"):
            results.append(_result(name, "SKIP", "server config did not load"))
    else:
        loaded: ServerConfig = cfg
        results.append(_guarded("database", lambda: check_database(loaded)))
        results.append(_guarded("listen-port", lambda: check_listen_port(loaded.host, loaded.port)))
        results.append(_guarded("tls", lambda: check_tls(loaded)))
        results.append(_guarded("auth-token", lambda: check_auth_token(loaded)))

    results.append(_guarded("audit-log", check_audit_log))
    results.append(_guarded("seal-key", check_seal_key))
    return results


def _guarded(name: str, check: Callable[[], PreflightResult]) -> PreflightResult:
    """A check that raised is reported as FAIL, so one bug never hides the rest."""
    try:
        return check()
    except Exception as exc:  # noqa: BLE001 — a diagnostic must not crash the report
        return _result(name, "FAIL", f"check errored: {type(exc).__name__}: {exc}")
