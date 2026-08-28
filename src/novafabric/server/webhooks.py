"""Webhook subscription registry store (ADR-0205 P1, experimental).

Persisted server-managed webhook subscriptions + delivery-attempt log, in the
same registry SQLite DB the sibling server stores use (``api_keys.py`` /
``workspace_store.py`` pattern: per-call connections, idempotent
``CREATE TABLE IF NOT EXISTS`` DDL).

Secret scheme (spec ``the private design/spec/webhook-registry-v0.md``):

- Full secret string ``nvwh_<hook_id>_<secret>`` (``secrets.token_urlsafe``),
  returned exactly ONCE by :func:`create_webhook` and never by any other
  endpoint, log line, audit entry, or delivery row.
- Unlike ``nvfk_`` API keys, the secret **cannot** be stored hash-only: the
  server must recover it to compute the delivery HMAC (ADR-0205 D3). At rest
  it is wrapped via a configured ADR-0185 ``KeyWrappingBackend``; with no
  backend configured it is stored as-is in the 0600 registry DB — an
  explicitly documented fallback surfaced as ``secret_at_rest`` in responses.

Delivery-level signature (Stripe-style, distinct from the ADR-0137 embedded
record signature): ``X-NovaFabric-Signature: t=<unix>,v1=<hex hmac-sha256>``
over ``"{t}." + raw_body_bytes``; receivers reject when ``|now - t| > 300 s``
(:func:`verify_delivery_signature` is the reference verifier).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from novafabric.capture._ulid import new_ulid
from novafabric.events.model import EventType
from novafabric.trust.novaseal.signing_backend import (
    KeyWrappingBackend,
    _aes_gcm_unwrap,
    _aes_gcm_wrap,
    _kek_fingerprint,
)

logger = logging.getLogger(__name__)

SECRET_PREFIX = "nvwh_"
_HOOK_ID_LEN = 8  # token_urlsafe(6) → exactly 8 urlsafe chars
_SECRET_BYTES = 32  # token_urlsafe(32) → 43 urlsafe chars

#: Receiver-side timestamp tolerance (seconds) — normative default (spec).
SIGNATURE_TOLERANCE_S = 300

#: Delivery-log payload column cap (bytes) — spec-normative.
PAYLOAD_CAP_BYTES = 64 * 1024

#: Retention defaults (spec-normative; config keys ``server.webhooks.*``).
DEFAULT_RETENTION_DAYS = 30
DEFAULT_RETENTION_ROWS = 10_000

#: Env var naming a local 256-bit KEK file (32 raw bytes or 64 hex chars) used
#: to wrap signing secrets at rest (ADR-0185 wrap path, documented fallback:
#: plaintext in the 0600 registry DB when unset).
KEK_PATH_ENV = "NOVAFABRIC_WEBHOOKS_KEK_PATH"

_TERMINAL_STATUSES = frozenset({"delivered", "failed", "dropped"})

_DDL_WEBHOOKS = """
CREATE TABLE IF NOT EXISTS webhooks (
    hook_id            TEXT PRIMARY KEY,
    url                TEXT NOT NULL,
    description        TEXT NOT NULL DEFAULT '',
    secret_ciphertext  TEXT NOT NULL,
    secret_kek_ref     TEXT,
    event_types        TEXT,
    workspace          TEXT,
    disabled           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    created_by         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
)
"""

_DDL_DELIVERIES = """
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id        TEXT PRIMARY KEY,
    hook_id            TEXT NOT NULL REFERENCES webhooks(hook_id),
    event_id           TEXT NOT NULL,
    event_type         TEXT NOT NULL,
    payload            TEXT NOT NULL,
    status             TEXT NOT NULL CHECK (status IN
                         ('pending','retrying','delivered','failed','dropped')),
    attempts           INTEGER NOT NULL DEFAULT 0,
    last_status_code   INTEGER,
    last_error         TEXT,
    next_attempt_at    TEXT,
    redelivery_of      TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
)
"""

_DDL_DELIVERIES_INDEX = """
CREATE INDEX IF NOT EXISTS ix_wd_hook_created
    ON webhook_deliveries(hook_id, created_at DESC)
"""

_HOOK_COLUMNS = (
    "hook_id, url, description, secret_kek_ref, event_types, workspace, "
    "disabled, created_at, created_by, updated_at"
)

_DELIVERY_COLUMNS = (
    "delivery_id, hook_id, event_id, event_type, payload, status, attempts, "
    "last_status_code, last_error, next_attempt_at, redelivery_of, "
    "created_at, updated_at"
)

_ERROR_TRUNCATE = 512


# ---------------------------------------------------------------------------
# Named exceptions
# ---------------------------------------------------------------------------


class UnknownWebhookError(KeyError):
    """Raised when a ``hook_id`` is not present in the registry."""


class UnknownDeliveryError(KeyError):
    """Raised when a ``delivery_id`` is not present in the delivery log."""


class InvalidWebhookUrlError(ValueError):
    """Raised for non-absolute, non-http(s), or insecure non-loopback URLs."""


class InvalidEventTypeError(ValueError):
    """Raised when an event-type filter names an unknown ``EventType``."""


class UnknownWorkspaceError(ValueError):
    """Raised when a webhook is scoped to a workspace that does not exist."""


class SecretUnavailableError(RuntimeError):
    """The stored secret is KEK-wrapped but no wrapping backend is configured."""


class NotRedeliverableError(ValueError):
    """Redelivery requested for a delivery row that is not terminal-failed."""


# ---------------------------------------------------------------------------
# Storage helpers (api_keys.py conventions: registry DB, per-call connection)
# ---------------------------------------------------------------------------


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_DDL_WEBHOOKS)
    conn.execute(_DDL_DELIVERIES)
    conn.execute(_DDL_DELIVERIES_INDEX)
    conn.commit()


def _get_conn(db_path: Path | None) -> sqlite3.Connection:
    from novafabric.registry.store import get_connection, get_db_path

    resolved = db_path or get_db_path()
    conn = get_connection(resolved)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(
    event_type_value: str,
    actor: str,
    resource_id: str,
    details: dict[str, Any],
    audit_log_path: Path | None,
) -> None:
    """Append one hash-chained audit entry (ADR-0205 / spec "Audit").

    ``details`` must never contain the signing secret or its ciphertext.
    """
    from novafabric.audit import AuditEventType, AuditLog, _paths

    path = audit_log_path or _paths.AUDIT_LOG_PATH
    AuditLog(path).append(
        event_type=AuditEventType(event_type_value),
        actor=actor,
        resource_id=resource_id,
        details=details,
    )


# ---------------------------------------------------------------------------
# Secret at rest: ADR-0185 wrap when a backend is configured, else plaintext
# ---------------------------------------------------------------------------


class _FileKekBackend:
    """Minimal :class:`KeyWrappingBackend` over a local 256-bit KEK file.

    Same KEK file format as ``LocalSigningBackend`` (32 raw bytes or 64 hex
    chars); dev/test parity only — production deployments should hold the KEK
    in a KMS via one of the cloud ``KeyWrappingBackend`` implementations in
    ``novafabric.trust.novaseal.signing_backend`` (ADR-0185).
    """

    def __init__(self, kek_path: Path) -> None:
        self._kek_path = kek_path

    def _load_kek(self) -> bytes:
        raw = self._kek_path.read_bytes()
        if len(raw) == 32:
            return raw
        text = raw.decode("ascii", errors="strict").strip() if len(raw) <= 66 else ""
        if len(text) == 64:
            return bytes.fromhex(text)
        raise ValueError(
            f"KEK file {self._kek_path} must contain exactly 32 raw bytes or "
            f"64 hex characters (got {len(raw)} bytes)"
        )

    def wrap_key(self, plaintext_key: bytes) -> bytes:
        return _aes_gcm_wrap(self._load_kek(), plaintext_key)

    def unwrap_key(self, wrapped_key: bytes) -> bytes:
        return _aes_gcm_unwrap(self._load_kek(), wrapped_key)

    def kek_ref(self) -> str:
        return f"local-kek:{_kek_fingerprint(self._load_kek())}"


def resolve_wrapping_backend() -> KeyWrappingBackend | None:
    """Return the configured secret-wrapping backend, or None (plaintext fallback).

    P1 wiring: a local KEK file named by ``NOVAFABRIC_WEBHOOKS_KEK_PATH``.
    The cloud-KMS backends (ADR-0185, ``novafabric.trust.novaseal.signing_backend``)
    are implemented but not yet dispatched to from here — selecting one for
    webhook secret storage is unwired follow-up work, not an infra gate.
    """
    raw = os.environ.get(KEK_PATH_ENV, "").strip()
    if not raw:
        return None
    return _FileKekBackend(Path(raw))


def _seal_secret(
    secret: str, backend: KeyWrappingBackend | None
) -> tuple[str, str | None]:
    """Return ``(ciphertext_text, kek_ref)`` for storing the secret at rest."""
    if backend is None:
        return secret, None
    wrapped = backend.wrap_key(secret.encode("utf-8"))
    return base64.b64encode(wrapped).decode("ascii"), backend.kek_ref()


def _open_secret(
    ciphertext: str, kek_ref: str | None, backend: KeyWrappingBackend | None
) -> str:
    if kek_ref is None:
        return ciphertext
    if backend is None:
        raise SecretUnavailableError(
            f"webhook secret is wrapped under {kek_ref!r} but no "
            f"KeyWrappingBackend is configured (set {KEK_PATH_ENV})"
        )
    return backend.unwrap_key(base64.b64decode(ciphertext)).decode("utf-8")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    import ipaddress

    name = host.strip().lower()
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _blocked_ssrf_addresses(host: str) -> list[str]:
    """Return the private/link-local/reserved addresses *host* maps to.

    Loopback is deliberately excluded — local http webhooks are a supported
    first-class feature. Literal IPs are checked directly; DNS names are
    resolved best-effort (an unresolvable name is not blocked here — delivery
    would fail loudly, and blocking on transient DNS failure is brittle).
    """
    import ipaddress
    import socket

    def _classify(ip_str: str) -> str | None:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return None
        if ip.is_loopback:
            return None
        if ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_unspecified:
            return ip_str
        return None

    name = host.strip().lower()
    literal = _classify(name)
    if literal is not None:
        return [literal]
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return []
    blocked: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        classified = _classify(addr)
        if classified is not None and classified not in blocked:
            blocked.append(classified)
    return blocked


def validate_url(
    url: str,
    *,
    allow_insecure_url: bool = False,
    allow_internal_targets: bool = False,
) -> str:
    """Validate a webhook endpoint URL per the spec; return it unchanged.

    Absolute ``http(s)`` only; ``https`` required for non-loopback hosts
    unless ``allow_insecure_url`` (``server.webhooks.allow_insecure_url``,
    default false — a documented, auditable opt-out).

    SSRF guard: hosts resolving to private, link-local, reserved, or
    unspecified addresses are rejected unless ``allow_internal_targets``
    (``server.webhooks.allow_internal_targets``, default false). Loopback
    remains permitted — local webhooks are a supported feature.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise InvalidWebhookUrlError(
            f"webhook url must be an absolute http(s) URL, got {url!r}"
        )
    if (
        parsed.scheme == "http"
        and not _is_loopback_host(parsed.hostname)
        and not allow_insecure_url
    ):
        raise InvalidWebhookUrlError(
            f"http:// is only allowed for loopback hosts, got {url!r} "
            f"(set server.webhooks.allow_insecure_url to override)"
        )
    if not allow_internal_targets:
        blocked = _blocked_ssrf_addresses(parsed.hostname)
        if blocked:
            raise InvalidWebhookUrlError(
                f"webhook host {parsed.hostname!r} resolves to a private/"
                f"link-local/reserved address ({', '.join(blocked)}); refused "
                f"to prevent SSRF (set server.webhooks.allow_internal_targets "
                f"to override)"
            )
    return url


def _validate_event_types(event_types: list[str] | None) -> list[str] | None:
    if event_types is None:
        return None
    valid = {t.value for t in EventType}
    unknown = sorted(set(event_types) - valid)
    if unknown:
        raise InvalidEventTypeError(
            f"unknown event type(s) {unknown}; valid values: {sorted(valid)}"
        )
    return sorted(set(event_types))


def _validate_workspace(workspace: str | None, db_path: Path | None) -> None:
    if workspace is None:
        return
    from novafabric.server import workspace_store

    for row in workspace_store.list_workspaces(db_path=db_path):
        if workspace in (row.get("slug"), row.get("id")):
            return
    raise UnknownWorkspaceError(f"workspace {workspace!r} does not exist")


# ---------------------------------------------------------------------------
# Secret format + delivery signature (Stripe-style t=...,v1=...)
# ---------------------------------------------------------------------------


def parse_secret(secret: str) -> tuple[str, str] | None:
    """Split a full ``nvwh_`` secret into ``(hook_id, secret_part)``; None if malformed.

    Positional parse (``nvwh_`` + 8 chars + ``_`` + secret) — the urlsafe
    alphabet means the ``hook_id`` itself may contain ``_``.
    """
    prefix_end = len(SECRET_PREFIX)
    sep_index = prefix_end + _HOOK_ID_LEN
    if not secret.startswith(SECRET_PREFIX) or len(secret) <= sep_index + 1:
        return None
    if secret[sep_index] != "_":
        return None
    hook_id = secret[prefix_end:sep_index]
    secret_part = secret[sep_index + 1 :]
    if not hook_id or not secret_part:
        return None
    return hook_id, secret_part


def sign_delivery(secret: str, body: bytes, timestamp: int) -> str:
    """Return the ``X-NovaFabric-Signature`` value ``t=<unix>,v1=<hex>``.

    Canonical signed string: ``"{t}" + "." + raw_body_bytes`` (spec-normative;
    the receiver verifies the exact bytes received, no re-canonicalization).
    """
    mac = hmac.new(
        secret.encode("utf-8"),
        str(timestamp).encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={timestamp},v1={mac}"


def verify_delivery_signature(
    secret: str,
    body: bytes,
    header: str,
    *,
    tolerance_s: int = SIGNATURE_TOLERANCE_S,
    now: int | None = None,
) -> bool:
    """Reference receiver-side verifier (spec-normative behavior).

    Rejects malformed headers, timestamps outside ``tolerance_s`` of *now*
    (replay protection), and HMAC mismatches (``hmac.compare_digest``).
    """
    parts = dict(
        p.split("=", 1) for p in header.split(",") if "=" in p
    )
    t_raw = parts.get("t")
    v1 = parts.get("v1")
    if not t_raw or not v1:
        return False
    try:
        timestamp = int(t_raw)
    except ValueError:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > tolerance_s:
        return False
    expected = sign_delivery(secret, body, timestamp)
    return hmac.compare_digest(expected, f"t={timestamp},v1={v1}")


# ---------------------------------------------------------------------------
# Subscription CRUD
# ---------------------------------------------------------------------------


def _row_to_record(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    d = dict(row)
    d.pop("secret_ciphertext", None)
    kek_ref = d.pop("secret_kek_ref", None)
    d["secret_at_rest"] = "wrapped" if kek_ref else "plaintext"
    d["disabled"] = bool(d["disabled"])
    raw_types = d.get("event_types")
    d["event_types"] = json.loads(raw_types) if raw_types else None
    return d


def create_webhook(
    url: str,
    *,
    actor: str,
    description: str = "",
    event_types: list[str] | None = None,
    workspace: str | None = None,
    disabled: bool = False,
    allow_insecure_url: bool = False,
    allow_internal_targets: bool = False,
    db_path: Path | None = None,
    audit_log_path: Path | None = None,
    wrapping_backend: KeyWrappingBackend | None = None,
) -> tuple[str, dict[str, Any]]:
    """Create a subscription. Returns ``(full_secret, record)``.

    The full ``nvwh_`` secret is returned ONCE here; at rest it is stored
    ADR-0185-wrapped when *wrapping_backend* is given, else as-is in the 0600
    registry DB (documented fallback — see ``secret_at_rest`` on the record).
    """
    validate_url(
        url,
        allow_insecure_url=allow_insecure_url,
        allow_internal_targets=allow_internal_targets,
    )
    type_list = _validate_event_types(event_types)
    _validate_workspace(workspace, db_path)

    now_iso = _now().isoformat()
    secret_part = secrets.token_urlsafe(_SECRET_BYTES)

    conn = _get_conn(db_path)
    try:
        for _ in range(5):
            hook_id = secrets.token_urlsafe(6)
            ciphertext, kek_ref = _seal_secret(
                f"{SECRET_PREFIX}{hook_id}_{secret_part}", wrapping_backend
            )
            try:
                conn.execute(
                    """
                    INSERT INTO webhooks
                        (hook_id, url, description, secret_ciphertext,
                         secret_kek_ref, event_types, workspace, disabled,
                         created_at, created_by, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hook_id,
                        url,
                        description,
                        ciphertext,
                        kek_ref,
                        json.dumps(type_list) if type_list is not None else None,
                        workspace,
                        1 if disabled else 0,
                        now_iso,
                        actor,
                        now_iso,
                    ),
                )
                conn.commit()
                break
            except sqlite3.IntegrityError:  # pragma: no cover — 48-bit collision
                continue
        else:  # pragma: no cover — bounded retry exhausted
            raise RuntimeError("could not allocate a unique webhook id")
        row = conn.execute(
            f"SELECT {_HOOK_COLUMNS} FROM webhooks WHERE hook_id = ?", (hook_id,)
        ).fetchone()
    finally:
        conn.close()

    _audit(
        "webhook.create",
        actor,
        hook_id,
        {
            "url": url,
            "event_types": type_list,
            "workspace": workspace,
            "disabled": disabled,
        },
        audit_log_path,
    )
    logger.info("Created webhook %s -> %s", hook_id, url)
    return f"{SECRET_PREFIX}{hook_id}_{secret_part}", _row_to_record(row)


def list_webhooks(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return subscription metadata rows — never secrets or ciphertexts."""
    conn = _get_conn(db_path)
    try:
        rows = conn.execute(
            f"SELECT {_HOOK_COLUMNS} FROM webhooks ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_record(row) for row in rows]


def get_webhook(hook_id: str, *, db_path: Path | None = None) -> dict[str, Any]:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            f"SELECT {_HOOK_COLUMNS} FROM webhooks WHERE hook_id = ?", (hook_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise UnknownWebhookError(f"webhook '{hook_id}' not found")
    return _row_to_record(row)


_UNSET: Any = object()


def update_webhook(
    hook_id: str,
    *,
    actor: str,
    url: str | Any = _UNSET,
    description: str | Any = _UNSET,
    event_types: list[str] | None | Any = _UNSET,
    workspace: str | None | Any = _UNSET,
    disabled: bool | Any = _UNSET,
    allow_insecure_url: bool = False,
    allow_internal_targets: bool = False,
    db_path: Path | None = None,
    audit_log_path: Path | None = None,
) -> dict[str, Any]:
    """Update url / description / event filter / workspace / disabled flag.

    The signing secret is deliberately NOT updatable (ADR-0205 D2 — rotation
    is a P2 slice with an overlap window, mirroring ADR-0193 D3).
    """
    updates: dict[str, Any] = {}
    changed: dict[str, Any] = {}
    if url is not _UNSET:
        validate_url(
            url,
            allow_insecure_url=allow_insecure_url,
            allow_internal_targets=allow_internal_targets,
        )
        updates["url"] = url
        changed["url"] = url
    if description is not _UNSET:
        updates["description"] = description
        changed["description"] = description
    if event_types is not _UNSET:
        type_list = _validate_event_types(event_types)
        updates["event_types"] = (
            json.dumps(type_list) if type_list is not None else None
        )
        changed["event_types"] = type_list
    if workspace is not _UNSET:
        _validate_workspace(workspace, db_path)
        updates["workspace"] = workspace
        changed["workspace"] = workspace
    if disabled is not _UNSET:
        updates["disabled"] = 1 if disabled else 0
        changed["disabled"] = bool(disabled)

    conn = _get_conn(db_path)
    try:
        if updates:
            updates["updated_at"] = _now().isoformat()
            assignments = ", ".join(f"{name} = ?" for name in updates)
            cur = conn.execute(
                f"UPDATE webhooks SET {assignments} WHERE hook_id = ?",  # noqa: S608
                (*updates.values(), hook_id),
            )
            conn.commit()
            if cur.rowcount == 0:
                raise UnknownWebhookError(f"webhook '{hook_id}' not found")
        row = conn.execute(
            f"SELECT {_HOOK_COLUMNS} FROM webhooks WHERE hook_id = ?", (hook_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise UnknownWebhookError(f"webhook '{hook_id}' not found")

    if changed:
        _audit("webhook.update", actor, hook_id, changed, audit_log_path)
    return _row_to_record(row)


def delete_webhook(
    hook_id: str,
    *,
    actor: str,
    db_path: Path | None = None,
    audit_log_path: Path | None = None,
) -> None:
    """Delete the subscription (delivery rows are retained until pruned)."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute("DELETE FROM webhooks WHERE hook_id = ?", (hook_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise UnknownWebhookError(f"webhook '{hook_id}' not found")
    finally:
        conn.close()
    _audit("webhook.delete", actor, hook_id, {}, audit_log_path)
    logger.info("Deleted webhook %s", hook_id)


def load_secret(
    hook_id: str,
    *,
    db_path: Path | None = None,
    wrapping_backend: KeyWrappingBackend | None = None,
) -> str:
    """Recover the full signing secret for delivery-time HMAC (server-internal).

    Never exposed over any route (ADR-0205 D3). Raises
    :class:`SecretUnavailableError` when the stored secret is wrapped but no
    backend is configured.
    """
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            "SELECT secret_ciphertext, secret_kek_ref FROM webhooks "
            "WHERE hook_id = ?",
            (hook_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise UnknownWebhookError(f"webhook '{hook_id}' not found")
    return _open_secret(
        row["secret_ciphertext"], row["secret_kek_ref"], wrapping_backend
    )


# ---------------------------------------------------------------------------
# Delivery log
# ---------------------------------------------------------------------------


def _truncate_error(error: str | None) -> str | None:
    if error is None:
        return None
    return error[:_ERROR_TRUNCATE]


def insert_delivery(
    hook_id: str,
    *,
    event_id: str,
    event_type: str,
    payload: str,
    status: str = "pending",
    next_attempt_at: str | None = None,
    redelivery_of: str | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    retention_rows: int = DEFAULT_RETENTION_ROWS,
    db_path: Path | None = None,
) -> str:
    """Insert one delivery row; prune retention opportunistically. Returns id.

    Payloads above :data:`PAYLOAD_CAP_BYTES` are stored truncated with a
    ``payload_truncated`` marker on ``last_error`` (the delivery itself still
    sends the full body — spec "Delivery-log retention").
    """
    delivery_id = new_ulid()
    now_iso = _now().isoformat()
    last_error: str | None = None
    if len(payload.encode("utf-8")) > PAYLOAD_CAP_BYTES:
        payload = payload.encode("utf-8")[:PAYLOAD_CAP_BYTES].decode(
            "utf-8", errors="ignore"
        )
        last_error = "payload_truncated"
    conn = _get_conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO webhook_deliveries
                (delivery_id, hook_id, event_id, event_type, payload, status,
                 attempts, last_status_code, last_error, next_attempt_at,
                 redelivery_of, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, ?, ?, ?, ?)
            """,
            (
                delivery_id,
                hook_id,
                event_id,
                event_type,
                payload,
                status,
                last_error,
                next_attempt_at,
                redelivery_of,
                now_iso,
                now_iso,
            ),
        )
        _prune_deliveries_conn(
            conn,
            hook_id,
            retention_days=retention_days,
            retention_rows=retention_rows,
        )
        conn.commit()
    finally:
        conn.close()
    return delivery_id


def _prune_deliveries_conn(
    conn: sqlite3.Connection,
    hook_id: str,
    *,
    retention_days: int,
    retention_rows: int,
) -> None:
    """Bounded retention: age cap + per-webhook row cap, terminal rows first."""
    cutoff = (_now() - timedelta(days=retention_days)).isoformat()
    terminal = tuple(sorted(_TERMINAL_STATUSES))
    conn.execute(
        f"DELETE FROM webhook_deliveries WHERE hook_id = ? AND created_at < ? "
        f"AND status IN ({','.join('?' * len(terminal))})",  # noqa: S608
        (hook_id, cutoff, *terminal),
    )
    count = conn.execute(
        "SELECT COUNT(*) AS c FROM webhook_deliveries WHERE hook_id = ?",
        (hook_id,),
    ).fetchone()["c"]
    excess = count - retention_rows
    if excess > 0:
        conn.execute(
            f"DELETE FROM webhook_deliveries WHERE delivery_id IN ("
            f"  SELECT delivery_id FROM webhook_deliveries"
            f"  WHERE hook_id = ? AND status IN ({','.join('?' * len(terminal))})"
            f"  ORDER BY created_at ASC LIMIT ?)",  # noqa: S608
            (hook_id, *terminal, excess),
        )


def prune_deliveries(
    hook_id: str,
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    retention_rows: int = DEFAULT_RETENTION_ROWS,
    db_path: Path | None = None,
) -> None:
    """Standalone retention pruning (same policy the insert path applies)."""
    conn = _get_conn(db_path)
    try:
        _prune_deliveries_conn(
            conn,
            hook_id,
            retention_days=retention_days,
            retention_rows=retention_rows,
        )
        conn.commit()
    finally:
        conn.close()


def record_attempt(
    delivery_id: str,
    *,
    status: str,
    status_code: int | None,
    error: str | None,
    next_attempt_at: str | None,
    db_path: Path | None = None,
) -> None:
    """Record one HTTP POST attempt on a delivery row (attempts += 1)."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE webhook_deliveries
            SET status = ?, attempts = attempts + 1, last_status_code = ?,
                last_error = ?, next_attempt_at = ?, updated_at = ?
            WHERE delivery_id = ?
            """,
            (
                status,
                status_code,
                _truncate_error(error),
                next_attempt_at,
                _now().isoformat(),
                delivery_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise UnknownDeliveryError(f"delivery '{delivery_id}' not found")
    finally:
        conn.close()


def mark_delivery(
    delivery_id: str,
    *,
    status: str,
    error: str | None = None,
    next_attempt_at: str | None = None,
    redelivery_of: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Set a delivery row's status without counting an HTTP attempt."""
    conn = _get_conn(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE webhook_deliveries
            SET status = ?, last_error = COALESCE(?, last_error),
                next_attempt_at = ?,
                redelivery_of = COALESCE(?, redelivery_of), updated_at = ?
            WHERE delivery_id = ?
            """,
            (
                status,
                _truncate_error(error),
                next_attempt_at,
                redelivery_of,
                _now().isoformat(),
                delivery_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise UnknownDeliveryError(f"delivery '{delivery_id}' not found")
    finally:
        conn.close()


def get_delivery(
    delivery_id: str, *, db_path: Path | None = None
) -> dict[str, Any]:
    conn = _get_conn(db_path)
    try:
        row = conn.execute(
            f"SELECT {_DELIVERY_COLUMNS} FROM webhook_deliveries "
            f"WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise UnknownDeliveryError(f"delivery '{delivery_id}' not found")
    return dict(row)


def list_deliveries(
    hook_id: str,
    *,
    status: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Delivery-attempt log for one webhook, newest first."""
    conn = _get_conn(db_path)
    try:
        if status is None:
            rows = conn.execute(
                f"SELECT {_DELIVERY_COLUMNS} FROM webhook_deliveries "
                f"WHERE hook_id = ? ORDER BY created_at DESC, delivery_id DESC",
                (hook_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_DELIVERY_COLUMNS} FROM webhook_deliveries "
                f"WHERE hook_id = ? AND status = ? "
                f"ORDER BY created_at DESC, delivery_id DESC",
                (hook_id, status),
            ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]
