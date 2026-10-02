# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Evidence-cart export — ADR-0239 D3/D5/D6/D8 (experimental).

``POST /api/evidence/cart/export`` turns the dashboard's session cart (a list of
**references**, held in the browser — D7) into **one** signed Evidence Bundle,
through the shipped pieces and nothing new:

* :meth:`novafabric.evidence.cart.EvidenceCart.resolve` reads every reference
  **once**, at one read point (D2), carrying unresolvable items *marked*, never
  dropped, and attaching active legal holds (D8);
* :class:`novafabric.evidence.bundle.CapsuleSetBundleBuilder` writes the bundle
  (ADR-0011 Amendment 1), with the curation record as a hashed ``curation.json``
  artifact (D5) — so the shipped ``nova verify`` checks it unmodified.

## Why each guard exists

* **``admin`` scope (D6).** Classified in ``serve.authz.ROUTE_SCOPES``; export
  takes evidence out of the system, the highest-consequence non-destructive
  action the dashboard offers.
* **No unaudited export.** The bundle is built under a ``.partial`` name, the
  hash-chained ``evidence.export`` entry (``audit.AuditLog``) is appended, and
  only then is the file moved into place. If the chained entry cannot be
  written the partial bundle is deleted and the export answers 503: an
  evidence export the audit trail cannot account for is exactly what D6 exists
  to prevent. The Layer-B dashboard audit (``serve.audit``) records it as well,
  so it shows in the Audit tab.
* **Unresolved is never silent (spec §2.1).** If any reference does not resolve
  the export stops with 409 and the per-item reasons, unless the caller sets
  ``accept_unresolved`` — in which case the omissions are recorded in
  ``curation.json``. There is no path that yields a silently smaller bundle.
* **Bounded (spec §2.1).** At most :data:`MAX_CART_ITEMS` items and
  :data:`MAX_CART_CAPSULE_BYTES` of capsule content per export, and one export
  at a time per server (429 otherwise): the build is synchronous file IO and
  zip compression, and two concurrent 256 MiB builds would starve the
  dashboard.
* **Holds are disclosed, never touched (D8).** Held evidence exports; the
  manifest carries per-item hold ids and a top-level
  ``contains_held_evidence``. Nothing here places, releases or modifies a hold.

## Scope of this slice — what resolves

``run`` and ``capsule`` items resolve to a capsule directory. The other cart
kinds (``lineage_query``, ``diff``, ``chart``, ``policy_decision``,
``audit_record``) have **no resolver yet**; they are accepted into the cart and
reported as unresolved with that reason, so the operator sees exactly what the
bundle does not contain. D4 (a chart carries its view state and query) is
**planned**.

A **one-run cart is exported** like any other: ``CapsuleSetBundleBuilder`` writes
``subject`` as the single-object form the schema allows (the array form needs
two or more), so the bundle validates and verifies. A cart that resolves to
*no* capsule is still refused (422).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

#: Hard cap on references per export (spec §2.1 "item count … bounded").
MAX_CART_ITEMS: Final[int] = 50

#: Hard cap on the summed size of the capsule directories one export copies.
#: 256 MiB keeps a synchronous in-request build well inside the dashboard's
#: request budget on a laptop disk; a larger set is a CLI job.
MAX_CART_CAPSULE_BYTES: Final[int] = 256 * 1024 * 1024

#: Kinds that resolve to a capsule directory in this slice.
RESOLVABLE_KINDS: Final[frozenset[str]] = frozenset({"run", "capsule"})

#: Environment override for the hash-chained audit log (same variable the
#: server's insecure-mode audit honours).
AUDIT_LOG_PATH_ENV: Final[str] = "NOVAFABRIC_AUDIT_LOG_PATH"

#: Where bundles land unless overridden (the Reports tab's evidence inventory
#: reads the same directory).
EVIDENCE_DIR_ENV: Final[str] = "NOVAFABRIC_EVIDENCE_DIR"


class CartItemIn(BaseModel):
    """One cart reference as the browser holds it (spec §1)."""

    kind: str = Field(max_length=32)
    ref: str = Field(min_length=1, max_length=512)
    added_at: str = Field(min_length=1, max_length=64)
    added_by: str | None = Field(default=None, max_length=256)
    added_from: str | None = Field(default=None, max_length=2048)
    note: str | None = Field(default=None, max_length=2000)


class CartExportRequest(BaseModel):
    cart_id: str | None = Field(default=None, max_length=64)
    items: list[CartItemIn] = Field(default_factory=list)
    confirmed: bool = False
    #: Proceed when some references did not resolve, recording the omissions.
    accept_unresolved: bool = False


class CartExportResponse(BaseModel):
    ok: bool
    bundle_path: str
    bundle_sha256: str
    size_bytes: int
    item_count: int
    capsule_count: int
    unresolved: list[dict[str, Any]]
    contains_held_evidence: bool
    audit_entry_hash: str
    cli_verify: str


def _audit_log_path() -> Path:
    from novafabric.audit import AUDIT_LOG_PATH

    env = os.environ.get(AUDIT_LOG_PATH_ENV, "").strip()
    return Path(env) if env else AUDIT_LOG_PATH


def _evidence_dir() -> Path:
    env = os.environ.get(EVIDENCE_DIR_ENV, "").strip()
    return Path(env) if env else Path.home() / ".novafabric" / "evidence"


def _default_key_path() -> Path:
    return Path.home() / ".novafabric" / "keys" / "local-key.pem"


def _ensure_signing_key(key_path: Path) -> bool:
    """Return True when the default key had to be generated (same rule as the
    per-run export: a missing *default* key is auto-generated and disclosed)."""
    if key_path.exists():
        return False
    from novafabric.evidence.signing import generate_keypair

    priv_path, pub_path = generate_keypair(key_path.parent)
    if priv_path != key_path:
        key_path.write_bytes(priv_path.read_bytes())
        try:
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        pub_alias = key_path.with_suffix(".pub.pem")
        if not pub_alias.exists():
            pub_alias.write_bytes(pub_path.read_bytes())
    return True


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _NotResolvable(Exception):
    """A cart kind with no resolver in this slice."""


def build_evidence_cart_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
    resolve_capsule: Callable[[str, Path], Path],
    server_token_fp: str,
    audit_append: Callable[..., dict[str, Any]],
) -> APIRouter:
    """Build the cart-export router.

    ``resolve_capsule`` is serve's own run-id → directory resolver (404 on an
    unknown id), injected so this module never imports ``serve.app``.
    ``server_token_fp`` lets the record say ``shared-token`` for the one shared
    credential and ``credential`` for an issued one (ADR-0231 D3) without this
    module seeing any token. ``audit_append`` is ``serve.audit.append``.
    """
    router = APIRouter(tags=["evidence-cart"])
    single_flight = asyncio.Lock()

    def _identity(actor_fp: str) -> tuple[str, str]:
        source = "shared-token" if actor_fp == server_token_fp else "credential"
        label = f"dashboard:{actor_fp}"
        return label, source

    def _export(body: CartExportRequest, actor_fp: str) -> dict[str, Any] | JSONResponse:
        from novafabric.audit import AuditEventType, AuditLog
        from novafabric.evidence.bundle import (
            CapsuleSetBundleBuilder,
            CapsuleValidationError,
            UnsafeSkipsError,
        )
        from novafabric.evidence.cart import (
            CURATION_DISCLOSURE,
            CartItem,
            CartItemKind,
            EvidenceCart,
            capsule_holds,
        )
        from novafabric.evidence.signing import LocalSigner
        from novafabric.policy import PolicyDeniedError

        actor_label, identity_source = _identity(actor_fp)

        cart = EvidenceCart(owner=actor_label)
        for raw in body.items:
            try:
                kind = CartItemKind(raw.kind)
            except ValueError as exc:
                allowed = ", ".join(k.value for k in CartItemKind)
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown cart item kind {raw.kind!r}; allowed: {allowed}",
                ) from exc
            cart.add(
                CartItem(
                    kind=kind,
                    ref=raw.ref,
                    # The cart lives in the browser (D7); who added an item is
                    # the client's claim. The authenticated exporter is recorded
                    # separately as ``assembled_by``.
                    added_by=raw.added_by or actor_label,
                    added_at=raw.added_at,
                    added_from=raw.added_from,
                    note=raw.note,
                )
            )

        dirs: dict[tuple[str, str], Path] = {}

        def _digest_for(item: CartItem) -> str | None:
            from novafabric.evidence.merkle import capsule_merkle_root

            if item.kind.value not in RESOLVABLE_KINDS:
                raise _NotResolvable(
                    f"'{item.kind.value}' items have no resolver yet; only run and "
                    "capsule references are exported in this release (planned)"
                )
            try:
                cdir = resolve_capsule(item.ref, capsule_dir)
            except HTTPException as exc:
                raise LookupError(str(exc.detail)) from exc
            dirs[item.identity()] = cdir
            return capsule_merkle_root(cdir)

        def _holds_for(item: CartItem) -> tuple[str, ...]:
            # Holds are registry-global (``<capsule base>/../registries/*``), so
            # the lookup takes serve's capsule *base* — the same directory the
            # holds router writes under and the delete route reads — not the
            # per-run directory, whose parent would name no registry at all.
            if item.identity() not in dirs:
                return ()
            return capsule_holds(capsule_dir)

        read_point = datetime.now(timezone.utc).isoformat()
        resolved = cart.resolve(
            resolved_at=read_point,
            resolved_by=actor_label,
            digest_for=_digest_for,
            holds_for=_holds_for,
        )
        unresolved = [item.as_dict() for item in resolved.unresolved]
        if unresolved and not body.accept_unresolved:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "unresolved_items",
                    "unresolved": unresolved,
                    "remedy": (
                        "remove the listed items from the cart, or export with "
                        "accept_unresolved=true to record them as omissions in "
                        "the bundle's curation record"
                    ),
                },
            )

        capsule_dirs = [
            dirs[(item.kind, item.ref)]
            for item in resolved.items
            if item.digest is not None and (item.kind, item.ref) in dirs
        ]
        # Two references (a run id and a capsule id) can name one directory;
        # the builder refuses a repeated subject, so collapse here and say so.
        unique_dirs: list[Path] = []
        for d in capsule_dirs:
            if d.resolve() not in {u.resolve() for u in unique_dirs}:
                unique_dirs.append(d)
        if not unique_dirs:
            raise HTTPException(
                status_code=422,
                detail=(
                    "the cart resolves to no capsule; there is nothing to export. "
                    "Add at least one run or capsule reference."
                ),
            )

        total_bytes = sum(_dir_bytes(d) for d in unique_dirs)
        if total_bytes > MAX_CART_CAPSULE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"the cart's capsules total {total_bytes} bytes; one dashboard "
                    f"export is bounded at {MAX_CART_CAPSULE_BYTES} bytes. Split the "
                    "cart, or export from the CLI."
                ),
            )

        contains_held = any(item.legal_holds for item in resolved.items)
        cart_id = body.cart_id or hashlib.sha256(
            json.dumps(
                [list(i.identity()) for i in cart.items], separators=(",", ":")
            ).encode()
        ).hexdigest()[:16]
        assembly_by = {"id": actor_label, "identity_source": identity_source}
        curation: dict[str, Any] = {
            **resolved.as_dict(),
            # Spec §2.2 manifest additions, carried in the hashed curation file.
            "assembly": {
                "method": "operator-curated",
                "cart_id": cart_id,
                "assembled_by": [assembly_by],
                "item_provenance": [
                    {
                        "kind": i.kind,
                        "ref": i.ref,
                        "added_at": i.added_at,
                        "added_by": i.added_by,
                        **({"added_from": i.added_from} if i.added_from else {}),
                    }
                    for i in resolved.items
                ],
            },
            "completeness": {"claim": "curated", "statement": CURATION_DISCLOSURE},
            "contains_held_evidence": contains_held,
        }

        key_path = _default_key_path()
        try:
            key_autogenerated = _ensure_signing_key(key_path)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=500,
                detail=f"signing key unavailable at {key_path}: {exc}",
            ) from exc

        out_dir = _evidence_dir()
        out_dir.mkdir(parents=True, exist_ok=True)
        final_path = out_dir / f"cart-{cart_id}-{read_point[:19].replace(':', '')}.zip"
        partial = final_path.with_suffix(".zip.partial")
        cli_verify = f"nova verify {final_path}"
        refs = [{"kind": i.kind, "ref": i.ref} for i in resolved.items]

        def _record_failure(error: str) -> None:
            audit_append(
                action="evidence_cart_export",
                args={"cart_id": cart_id, "items": refs},
                cli_equivalent=cli_verify,
                actor_token_fp=actor_fp,
                result="error",
                error=error,
                identity_source=identity_source,
                actor_id=actor_label,
                required_scope="admin",
            )

        try:
            CapsuleSetBundleBuilder(
                unique_dirs,
                LocalSigner(key_path),
                partial,
                actor=actor_label,
                curation=curation,
            ).build()
        except PolicyDeniedError as exc:
            partial.unlink(missing_ok=True)
            _record_failure(f"policy denied: {exc}")
            raise HTTPException(status_code=403, detail=f"policy denied export: {exc}") from exc
        except UnsafeSkipsError as exc:
            partial.unlink(missing_ok=True)
            _record_failure(str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except CapsuleValidationError as exc:
            partial.unlink(missing_ok=True)
            _record_failure(str(exc))
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        bundle_sha = _sha256_file(partial)
        size = partial.stat().st_size
        details = {
            "cart_id": cart_id,
            "bundle_sha256": bundle_sha,
            "bundle_path": str(final_path),
            "size_bytes": size,
            "items": refs,
            "unresolved_count": len(unresolved),
            "contains_held_evidence": contains_held,
            "legal_holds": curation["legal_holds_present"],
            "identity_source": identity_source,
            "read_point": read_point,
            "adr": "ADR-0239",
        }
        try:
            entry = AuditLog(_audit_log_path()).append(
                event_type=AuditEventType.EVIDENCE_EXPORT,
                actor=actor_label,
                resource_id=f"sha256:{bundle_sha}",
                details=details,
            )
        except Exception as exc:  # noqa: BLE001 — no audit, no export
            partial.unlink(missing_ok=True)
            logger.error("evidence cart export refused: chained audit append failed: %s", exc)
            _record_failure(f"audit append failed: {type(exc).__name__}")
            return JSONResponse(
                status_code=503,
                content={
                    "error": "audit_unavailable",
                    "detail": (
                        "the export was built but the hash-chained audit entry could "
                        "not be written, so the bundle was deleted; an unaudited "
                        "evidence export is refused (ADR-0239 D6)"
                    ),
                    "remedy": f"make the audit log writable or set {AUDIT_LOG_PATH_ENV}",
                },
            )
        os.replace(partial, final_path)

        extra: dict[str, Any] = {
            "bundle_sha256": bundle_sha,
            "size_bytes": size,
            "audit_entry_hash": entry.entry_hash,
            "contains_held_evidence": contains_held,
        }
        if key_autogenerated:
            extra["key_autogenerated_at"] = str(key_path)
        audit_append(
            action="evidence_cart_export",
            args={"cart_id": cart_id, "items": refs},
            cli_equivalent=cli_verify,
            actor_token_fp=actor_fp,
            extra=extra,
            identity_source=identity_source,
            actor_id=actor_label,
            resource=f"sha256:{bundle_sha}",
            required_scope="admin",
        )
        return {
            "ok": True,
            "bundle_path": str(final_path),
            "bundle_sha256": bundle_sha,
            "size_bytes": size,
            "item_count": len(resolved.items),
            "capsule_count": len(unique_dirs),
            "unresolved": unresolved,
            "contains_held_evidence": contains_held,
            "legal_holds": curation["legal_holds_present"],
            "audit_entry_hash": entry.entry_hash,
            "key_autogenerated": key_autogenerated,
            "cli_verify": cli_verify,
        }

    @router.post(
        "/api/evidence/cart/export",
        operation_id="dashboardExportEvidenceCart",
        responses={200: {"model": CartExportResponse}},
        response_model=None,
    )
    async def export_cart(
        body: CartExportRequest = Body(...),
        actor_fp: str = Depends(verify_token),
    ) -> Any:
        """Export the session cart as one signed Evidence Bundle (ADR-0239). Experimental."""
        if not body.confirmed:
            raise HTTPException(
                status_code=400, detail="confirmation required (set confirmed=true)"
            )
        if not body.items:
            raise HTTPException(status_code=422, detail="the cart is empty")
        if len(body.items) > MAX_CART_ITEMS:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{len(body.items)} items; one export is bounded at "
                    f"{MAX_CART_ITEMS} items. Split the cart."
                ),
            )
        if single_flight.locked():
            raise HTTPException(
                status_code=429,
                detail="another evidence-cart export is running; retry when it finishes",
            )
        async with single_flight:
            return await asyncio.to_thread(_export, body, actor_fp)

    return router
