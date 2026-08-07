"""Jurisdiction-aware storage routing (ADR-0247 slice 1).

ADR-0077 shipped the *proof* layer (site seals, `ResidencyPolicy`) and left
placement as declared future design; nothing decided **where bytes land**.
This decorator decides it, with the slice-1 granularity the integration
audit settled on: a **tenant → jurisdiction** map (tenant resolved from the
``capsules/{tenant}/…`` CAS layout, exactly like per-tenant KEKs), routing
each write to the backend registered for that jurisdiction.

The load-bearing rule is **fail closed at write time**: a jurisdiction with
no registered backend refuses the write with a typed error — a
mis-configured region never silently lands elsewhere. Tenants without a
mapping use the default pool: existing single-store deployments observe zero
change.

Read-side residency enforcement wires ADR-0077's shipped-but-previously
unconsumed :func:`check_cross_jurisdiction_read` gate:
:meth:`JurisdictionRoutingAdapter.get_object_checked` denies cross-border
reads per the directional policy (deny by default), with the denial reason
carried in the error.

Per-capsule labels (capture-time `--jurisdiction` into the capsule `labels`
block) and the lineage jurisdiction filter are the next slices.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

import yaml

from novafabric.object_capsule_store.worm.base import WormAdapter, WormPutResult
from novafabric.trust.tenant_keys import tenant_from_object_key

if TYPE_CHECKING:
    from pathlib import Path

    from novafabric.compliance.sovereignty import ResidencyPolicy


class JurisdictionRoutingError(Exception):
    """A write could not be placed in its required jurisdiction (fail closed)."""


class ResidencyReadDenied(PermissionError):
    """A cross-jurisdiction read was refused by the residency policy."""


class JurisdictionRoutingAdapter(WormAdapter):
    """Route object writes/reads to per-jurisdiction backends.

    Args:
        routes:               jurisdiction tag → backend for that region.
        tenant_jurisdictions: tenant → jurisdiction tag (ADR-0247 D2's
                              tenant-level default; per-capsule override is a
                              later slice).
        default:              backend for tenants with no mapping (today's
                              behavior). ``None`` makes an unmapped tenant's
                              write fail closed too.
        residency_policy:     optional ADR-0077 policy consulted by
                              :meth:`get_object_checked`.
    """

    def __init__(
        self,
        routes: dict[str, WormAdapter],
        tenant_jurisdictions: dict[str, str],
        default: WormAdapter | None = None,
        residency_policy: "ResidencyPolicy | None" = None,
    ) -> None:
        self._routes = dict(routes)
        self._tenant_jurisdictions = dict(tenant_jurisdictions)
        self._default = default
        self._policy = residency_policy

    # ------------------------------------------------------------------ #
    # Resolution

    def jurisdiction_for_key(self, key: str) -> str | None:
        tenant = tenant_from_object_key(key)
        if tenant is None:
            return None
        return self._tenant_jurisdictions.get(tenant)

    def _backend_for_key(self, key: str) -> WormAdapter:
        jurisdiction = self.jurisdiction_for_key(key)
        if jurisdiction is None:
            if self._default is None:
                raise JurisdictionRoutingError(
                    f"no jurisdiction mapping for key {key!r} and no default "
                    "backend configured — refusing to place the object (ADR-0247)"
                )
            return self._default
        backend = self._routes.get(jurisdiction)
        if backend is None:
            raise JurisdictionRoutingError(
                f"key {key!r} requires jurisdiction {jurisdiction!r} but no backend "
                f"is registered for it (have: {sorted(self._routes)}) — refusing to "
                "place the object elsewhere (ADR-0247 fail-closed rule)"
            )
        return backend

    # ------------------------------------------------------------------ #
    # Write path

    def put_object(
        self,
        key: str,
        data: bytes,
        sha256_hex: str,
        retention_days: int,
        content_type: str = "application/octet-stream",
    ) -> WormPutResult:
        return self._backend_for_key(key).put_object(
            key, data, sha256_hex, retention_days, content_type
        )

    def apply_identical(
        self,
        key_a: str,
        data_a: bytes,
        sha256_a: str,
        key_b: str,
        data_b: bytes,
        sha256_b: str,
        retention_days: int,
    ) -> tuple[WormPutResult, WormPutResult]:
        backend_a = self._backend_for_key(key_a)
        backend_b = self._backend_for_key(key_b)
        if backend_a is not backend_b:
            raise JurisdictionRoutingError(
                "apply_identical spans two jurisdictions "
                f"({key_a!r} vs {key_b!r}) — an identical-WORM pair must live in "
                "one region"
            )
        return backend_a.apply_identical(
            key_a, data_a, sha256_a, key_b, data_b, sha256_b, retention_days
        )

    def put_log_object(self, key: str, data: bytes) -> str:
        return self._backend_for_key(key_or_default(key)).put_log_object(key, data)

    def put_log_object_if_absent(self, key: str, data: bytes) -> str:
        return self._backend_for_key(key_or_default(key)).put_log_object_if_absent(
            key, data
        )

    # ------------------------------------------------------------------ #
    # Read path

    def get_object(self, key: str) -> bytes:
        return self._backend_for_key(key).get_object(key)

    def get_object_checked(self, key: str, reader_jurisdiction: str) -> bytes:
        """Read *key* through the ADR-0077 residency gate.

        Same-jurisdiction reads always pass; cross-jurisdiction reads need a
        directional grant in the policy (deny by default). No policy
        configured + no jurisdiction on the key → plain read (nothing to
        enforce)."""
        data_jurisdiction = self.jurisdiction_for_key(key)
        if data_jurisdiction is not None and self._policy is not None:
            from novafabric.compliance.sovereignty import check_cross_jurisdiction_read

            decision = check_cross_jurisdiction_read(
                data_jurisdiction, reader_jurisdiction, self._policy
            )
            if not decision.allowed:
                raise ResidencyReadDenied(
                    f"read of {key!r} ({data_jurisdiction}) from "
                    f"{reader_jurisdiction!r} denied: {decision.reason}"
                )
        return self.get_object(key)

    def object_exists(self, key: str) -> bool:
        return self._backend_for_key(key).object_exists(key)

    def delete_object(self, key: str) -> None:
        self._backend_for_key(key).delete_object(key)

    def list_objects(self, prefix: str) -> list[str]:
        return sorted(self._each_backend_result("list_objects", prefix))

    def iter_objects(self, prefix: str) -> Iterator[str]:
        yield from self.list_objects(prefix)

    def _each_backend_result(self, method: str, prefix: str) -> list[str]:
        seen: set[str] = set()
        backends = list(self._routes.values())
        if self._default is not None:
            backends.append(self._default)
        for backend in backends:
            for key in getattr(backend, method)(prefix):
                seen.add(key)
        return list(seen)


def key_or_default(key: str) -> str:
    """Log/namespace keys (``_capsule_log/{tenant}/…``) carry the tenant in the
    second segment; normalize them to a capsule-shaped key so log objects
    co-locate with their tenant's data."""
    parts = key.split("/")
    if len(parts) >= 3 and parts[0] == "_capsule_log":
        return f"capsules/{parts[1]}/{'/'.join(parts[2:])}"
    return key


def load_jurisdiction_config(path: "Path") -> tuple[dict[str, str], "ResidencyPolicy"]:
    """Load the tenant→jurisdiction map and residency policy from one YAML file.

    Shape::

        tenants:
          acme: EU
          globex: US
        allow_cross_read:
          EU: [CH]          # CH readers may read EU data (directional)

    The ADR-0077 :class:`ResidencyPolicy` finally gains its loader here.
    """
    from novafabric.compliance.sovereignty import ResidencyPolicy

    raw = yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"jurisdiction config {path} must be a mapping")
    tenants = raw.get("tenants") or {}
    if not isinstance(tenants, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in tenants.items()
    ):
        raise ValueError(f"{path}: 'tenants' must map tenant → jurisdiction tag")
    policy = ResidencyPolicy(allow_cross_read=raw.get("allow_cross_read") or {})
    return dict(tenants), policy
