"""Per-tenant KEK registry (ADR-0243 slice 1).

One level added to the ADR-0185 hierarchy: a tenant's objects wrap their DEKs
under that tenant's KEK instead of the shared one. Tenants **without** a
configured key resolve to the default backend — existing flat-mode
deployments observe zero change (ADR-0243 D1), and old envelopes (no
``tenant_key_id``) stay readable forever.

Slice 1 carries the local-file profile: ``<kek_dir>/<tenant>.kek`` (32 raw
bytes or 64 hex chars, same format as ``NOVA_OBJECT_STORE_KEK_PATH``).
Removing a tenant's KEK file makes that tenant's data cryptographically
unreadable — reads fail closed with :class:`DekUnwrapError` naming the
tenant, which is the revocation semantic, not an error to paper over.
Cloud BYOK (a tenant KEK in the customer's KMS account) reuses the same
registry surface in a later slice.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from novafabric.trust.envelope_encryption import DekUnwrapError, EncryptedBlob
from novafabric.trust.novaseal.signing_backend import (
    KeyWrappingBackend,
    LocalSigningBackend,
)

_TENANT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def tenant_from_object_key(key: str) -> str | None:
    """Tenant segment of a capsule object key (``capsules/{tenant}/…``).

    The CAS layout (``object_capsule_store/cas.py``) already encodes the
    tenant as the first path segment under ``capsules/``; anything else
    (log objects, foreign keys) resolves to ``None`` → default backend.
    """
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "capsules" and _TENANT_NAME.match(parts[1]):
        return parts[1]
    return None


class TenantKeyRegistry:
    """Resolve wrap/unwrap backends per tenant, defaulting to the flat KEK."""

    def __init__(
        self,
        default_backend: KeyWrappingBackend,
        kek_dir: Path | None = None,
    ) -> None:
        self._default = default_backend
        self._kek_dir = kek_dir
        self._cache: dict[str, KeyWrappingBackend] = {}
        self._lock = threading.Lock()

    def _tenant_kek_path(self, tenant: str) -> Path | None:
        if self._kek_dir is None or not _TENANT_NAME.match(tenant):
            return None
        return self._kek_dir / f"{tenant}.kek"

    def _tenant_backend(self, tenant: str) -> KeyWrappingBackend | None:
        """The tenant's backend if a KEK is configured for it, else ``None``.

        Backends are cached per tenant; the existence check is re-done on a
        cache miss only — revocation is enforced at unwrap time (below),
        where it matters.
        """
        path = self._tenant_kek_path(tenant)
        if path is None:
            return None
        with self._lock:
            cached = self._cache.get(tenant)
            if cached is not None:
                return cached
            if not path.is_file():
                return None
            backend = LocalSigningBackend(path, path, kek_path=path)
            self._cache[tenant] = backend
            return backend

    # ------------------------------------------------------------------ #

    def backend_for_write(self, tenant: str | None) -> tuple[KeyWrappingBackend, str | None]:
        """The wrap backend for *tenant* plus the ``tenant_key_id`` to record.

        ``(default, None)`` when the tenant has no configured key — flat-mode
        compatibility by construction.
        """
        if tenant is not None:
            backend = self._tenant_backend(tenant)
            if backend is not None:
                return backend, tenant
        return self._default, None

    def backend_for_read(self, blob: EncryptedBlob) -> KeyWrappingBackend:
        """The unwrap backend for *blob*, failing closed on a revoked tenant key.

        A blob that records a ``tenant_key_id`` whose KEK no longer exists is
        *cryptographically unreadable by design* (ADR-0243 D3): raise
        :class:`DekUnwrapError` naming the tenant rather than falling back to
        the default KEK (which could not unwrap it anyway, but a clear error
        beats an ``InvalidTag`` mystery).
        """
        tenant = blob.tenant_key_id
        if tenant is None:
            return self._default
        path = self._tenant_kek_path(tenant)
        if path is not None and not path.is_file():
            with self._lock:
                self._cache.pop(tenant, None)
            raise DekUnwrapError(
                f"tenant KEK for {tenant!r} is absent (revoked or shredded) — "
                "this object is cryptographically unreadable by design (ADR-0243)"
            )
        backend = self._tenant_backend(tenant)
        if backend is None:
            raise DekUnwrapError(
                f"blob records tenant_key_id={tenant!r} but no tenant KEK directory "
                "is configured — cannot resolve the wrapping key"
            )
        return backend
