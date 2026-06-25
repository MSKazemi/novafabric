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
"""ObjectCapsuleStore — 5-step put_capsule() protocol (cap-002, FR-06).

Protocol (strict order — a kill between any two steps leaves the capsule
invisible to ``list_capsules()``):

    Step 1. Compute canonical SHA-256 and validate against caller assertion.
    Step 2. PutObject data with WORM policy.
    Step 3. NovaSeal.sign(sha256) → NovaSealLeaf   [synchronous, p99 < 200 ms]
    Step 4. PutObject NovaSeal sibling JSON at
            capsules/<tenant>/<sha[:4]>/<sha>/novaseal.json
            (identical WORM policy via WormAdapter.apply_identical()).
    Step 5. Append chain commit → capsule becomes visible.

If Step 3 raises ``NovaSealUnavailable``:
    - Enqueue to Local WAL (fsync-durable before returning).
    - Return ``CapsuleReceipt(status=PENDING_SIGNATURE)``.
    - Chain log commits == 0.
    - WAL row count == 1.

Idempotency (FR-08):
    If the data object already exists (same sha256 key), steps 2 and 4 are
    skipped (the object is already uploaded).  Only one chain commit is written.

OTel trace propagation:
    Propagates OpenTelemetry trace context from the upstream Event Envelope v1
    (FR: NFR-O02).  Uses stdlib ``opentelemetry.trace`` if available; no-ops
    gracefully if not installed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from novafabric.object_capsule_store.cas import (
    compute_sha256,
    derive_key,
    derive_novaseal_key,
    validate_sha256,
)
from novafabric.object_capsule_store.checkpoint import CheckpointCompactor
from novafabric.object_capsule_store.exceptions import (
    CapsuleIntegrityError,
    CompressionDictNotFound,
    NovaSealUnavailable,
)
from novafabric.object_capsule_store.local_wal import LocalWal
from novafabric.object_capsule_store.manifest_chain import ManifestChainWriter
from novafabric.object_capsule_store.models import CapsuleReceipt, ReceiptStatus
from novafabric.object_capsule_store.novaseal_client import NovaSealClient
from novafabric.object_capsule_store.worm.base import WormAdapter
from novafabric.object_capsule_store.zstd_dict import ZstdDictRegistry

log = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 1825  # 5 years (MiFID II / SEC 17a-4)
_DEFAULT_CHECKPOINT_EVERY = 1_000


class ObjectCapsuleStore:
    """Content-addressed, WORM-backed, NovaSeal-signed capsule store.

    The store is content-agnostic — it stores opaque bytes addressed by
    SHA-256.  Payload schema validation is the responsibility of upstream
    collectors (cross-study contract with parent-child capsule study).

    Args:
        adapter:         ``WormAdapter`` instance for the backend.
        novaseal_client: ``NovaSealClient`` wrapping the existing NovaSeal.
        wal:             ``LocalWal`` instance for WAL fallback.
        retention_days:  Default WORM retention in calendar days.
        checkpoint_every: Emit checkpoint every N commits.
    """

    def __init__(
        self,
        adapter: WormAdapter,
        novaseal_client: NovaSealClient,
        wal: LocalWal,
        retention_days: int = _DEFAULT_RETENTION_DAYS,
        checkpoint_every: int = _DEFAULT_CHECKPOINT_EVERY,
        zstd_registry: ZstdDictRegistry | None = None,
    ) -> None:
        self._adapter = adapter
        self._novaseal = novaseal_client
        self._wal = wal
        self._retention_days = retention_days
        self._chain = ManifestChainWriter(adapter)
        self._compactor = CheckpointCompactor(adapter, checkpoint_every=checkpoint_every)
        self._zstd = zstd_registry

    # -----------------------------------------------------------------------
    # put_capsule() — 5-step protocol
    # -----------------------------------------------------------------------

    def put_capsule(
        self,
        tenant: str,
        run_id: str,
        content_bytes: bytes,
        asserted_sha256: str | None = None,
        stats: dict[str, Any] | None = None,
        otel_context: Any | None = None,
        compression_dict_id: str | None = None,
    ) -> CapsuleReceipt:
        """Store *content_bytes* as a WORM-locked, NovaSeal-signed capsule.

        Args:
            tenant:             Tenant identifier.
            run_id:             Run identifier (per-run chain sharding, FR-05).
            content_bytes:      Raw capsule payload bytes (opaque — not parsed).
            asserted_sha256:    Caller-supplied SHA-256 hex.  If provided, must
                                match computed SHA-256 (FR-14).  If omitted,
                                computed value is used.  When *compression_dict_id*
                                is set, the assertion is checked against the
                                **uncompressed** bytes before compression.
            stats:              Optional stats dict forwarded to the chain commit.
            otel_context:       OpenTelemetry context for trace propagation
                                (FR-O02).
            compression_dict_id: If not None, compress *content_bytes* using the
                                 named zstd dictionary from the registry supplied
                                 at construction time.  The SHA-256 stored in the
                                 chain is of the **compressed** bytes so that the
                                 store remains content-agnostic (cap-005).
                                 Requires a ``ZstdDictRegistry`` to have been
                                 passed to the constructor.

        Returns:
            ``CapsuleReceipt`` with status ``OK`` or ``PENDING_SIGNATURE``.

        Raises:
            CapsuleHashMismatch: if *asserted_sha256* does not match computed
                                 (FR-14), raised BEFORE any network call.
            CompressionDictNotFound: if *compression_dict_id* is set but not
                                     present in the registry, or no registry was
                                     provided.
        """
        span = _otel_start_span("put_capsule", otel_context)
        try:
            return self._put_capsule_inner(
                tenant, run_id, content_bytes, asserted_sha256, stats, compression_dict_id
            )
        finally:
            _otel_end_span(span)

    def _put_capsule_inner(
        self,
        tenant: str,
        run_id: str,
        content_bytes: bytes,
        asserted_sha256: str | None,
        stats: dict[str, Any] | None,
        compression_dict_id: str | None = None,
    ) -> CapsuleReceipt:
        # ----
        # Step 0 (compression): If a compression_dict_id is provided, validate
        # the asserted SHA-256 against the *uncompressed* bytes, then compress
        # so that all subsequent steps (SHA-256 key derivation, PutObject, chain
        # commit) operate on the compressed bytes (cap-005).
        # ----
        if compression_dict_id is not None:
            if self._zstd is None:
                raise CompressionDictNotFound(compression_dict_id)
            # Validate against uncompressed bytes before transforming.
            if asserted_sha256 is not None:
                validate_sha256(content_bytes, asserted_sha256)
            # Compress; further steps use the compressed payload exclusively.
            content_bytes = self._zstd.compress(content_bytes, compression_dict_id)
            # SHA-256 assertion has been checked; reset so step 1 re-computes
            # against compressed bytes without raising a spurious mismatch.
            asserted_sha256 = None

        # ----
        # Step 1: Compute canonical SHA-256 (validate against assertion).
        # Raises CapsuleHashMismatch BEFORE any network call (FR-14).
        # ----
        if asserted_sha256 is not None:
            sha256 = validate_sha256(content_bytes, asserted_sha256)
        else:
            sha256 = compute_sha256(content_bytes)

        data_key = derive_key(tenant, sha256)
        novaseal_key = derive_novaseal_key(tenant, sha256)

        # ----
        # Step 2: PutObject data with WORM policy.
        # Idempotent: skip if already uploaded (FR-08).
        # ----
        if not self._adapter.object_exists(data_key):
            self._adapter.put_object(
                key=data_key,
                data=content_bytes,
                sha256_hex=sha256,
                retention_days=self._retention_days,
            )
            log.debug("Step 2 complete: data uploaded key=%s", data_key)
        else:
            log.debug("Step 2 skipped (idempotent): key=%s", data_key)

        # ----
        # Step 3: NovaSeal.sign(sha256) — synchronous.
        # On NovaSealUnavailable → WAL fallback, return PENDING_SIGNATURE.
        # ----
        try:
            leaf = self._novaseal.sign(sha256)
            novaseal_leaf_hash = leaf.leaf_hash
            signed_at = leaf.signed_at

            # Build the NovaSeal sibling JSON
            novaseal_payload = json.dumps({
                "capsule_sha256": sha256,
                "leaf_hash": novaseal_leaf_hash,
                "signed_at": signed_at,
                "dsse_envelope": leaf.dsse_envelope_bytes.decode("utf-8"),
            }, separators=(",", ":")).encode("utf-8")
            novaseal_sha256 = compute_sha256(novaseal_payload)

            log.debug("Step 3 complete: NovaSeal signed leaf_hash=%s", novaseal_leaf_hash)

        except NovaSealUnavailable as exc:
            log.warning("NovaSeal unavailable — falling back to WAL: %s", exc)
            row_id = self._wal.enqueue(data_key, sha256, tenant, run_id)
            return CapsuleReceipt(
                capsule_uri=data_key,
                capsule_sha256=sha256,
                status=ReceiptStatus.PENDING_SIGNATURE,
                wal_row_id=row_id,
            )

        # ----
        # Step 4: PutObject NovaSeal sibling JSON (identical WORM policy).
        # Use apply_identical() only if neither object exists.
        # Idempotent: skip if sibling already uploaded (FR-08).
        # ----
        if not self._adapter.object_exists(novaseal_key):
            # Use apply_identical to enforce identical WORM policy (FR-07).
            # Data may already exist (uploaded in step 2 of a prior run);
            # in that case upload only the sibling.
            if not self._adapter.object_exists(data_key):
                self._adapter.apply_identical(
                    key_a=data_key,
                    data_a=content_bytes,
                    sha256_a=sha256,
                    key_b=novaseal_key,
                    data_b=novaseal_payload,
                    sha256_b=novaseal_sha256,
                    retention_days=self._retention_days,
                )
            else:
                self._adapter.put_object(
                    key=novaseal_key,
                    data=novaseal_payload,
                    sha256_hex=novaseal_sha256,
                    retention_days=self._retention_days,
                    content_type="application/json",
                )
            log.debug("Step 4 complete: NovaSeal sibling uploaded key=%s", novaseal_key)
        else:
            log.debug("Step 4 skipped (idempotent): key=%s", novaseal_key)

        # ----
        # Step 5: Append chain commit — capsule becomes visible (FR-06).
        # ----
        commit = self._chain.append(
            tenant=tenant,
            run_id=run_id,
            capsule_uri=data_key,
            capsule_sha256=sha256,
            novaseal_leaf_hash=novaseal_leaf_hash,
            signed_at=signed_at,
            stats=stats or {},
            compression_dict_id=compression_dict_id,
        )
        log.debug("Step 5 complete: chain commit version=%d", commit.version)

        # Periodic checkpoint (fire-and-forget; errors are logged, not raised)
        try:
            self._compactor.maybe_checkpoint(tenant, run_id)
        except Exception as exc:
            log.warning("Checkpoint failed (non-fatal): %s", exc)

        return CapsuleReceipt(
            capsule_uri=data_key,
            capsule_sha256=sha256,
            version=commit.version,
            status=ReceiptStatus.OK,
            novaseal_leaf_hash=novaseal_leaf_hash,
            wal_row_id=None,
        )

    # -----------------------------------------------------------------------
    # get_capsule() — read path with SHA-256 pin verification
    # -----------------------------------------------------------------------

    def get_capsule(
        self,
        tenant: str,
        run_id: str,
        capsule_uri: str,
        verify_chain: bool = True,
    ) -> bytes:
        """Fetch capsule bytes and verify SHA-256 against the manifest chain pin.

        What is verified:
          - The manifest chain for (tenant, run_id) contains a 'put' commit for
            capsule_uri with a trusted capsule_sha256 pin written at seal time.
          - SHA-256(fetched bytes) == chain pin.
          - By default (``verify_chain=True``): the full ``prev_commit_hash``
            chain is walked and every link is verified (OQ-027).  A corrupted or
            truncated chain will raise ``ChainIntegrityError``.  This makes the
            read O(N) in the number of commits.

        What is NOT verified here:
          - NovaSeal cryptographic signature (use nova verify for that).
          - WORM retention policy (enforced at write time by the backend adapter).

        Args:
            tenant:       Tenant identifier.
            run_id:       Run identifier.
            capsule_uri:  URI of the specific capsule object to fetch.
            verify_chain: When True (the default), walk the entire
                          ``prev_commit_hash`` chain and raise
                          ``ChainIntegrityError`` if any link is broken (OQ-027).
                          Pass ``verify_chain=False`` only in performance-critical
                          paths where chain integrity has already been established
                          (e.g., bulk rebuild scans that verify the chain once
                          before iterating over individual capsules).

        Raises:
            KeyError: if capsule_uri has no committed 'put' entry in the chain.
            CapsuleIntegrityError: if fetched bytes SHA-256 != chain pin.
            ChainIntegrityError: if the ``prev_commit_hash`` chain is broken
                                 (raised by default; suppress with
                                 ``verify_chain=False``).
        """
        # read_chain() enforces version-contiguity and hash-chain linkage when
        # verify_integrity=True.  verify_chain defaults to True (OQ-027) so every
        # normal read walks the full prev_commit_hash chain.  Pass verify_chain=False
        # only for performance-critical bulk paths that have already verified the chain.
        commits = self._chain.read_chain(tenant, run_id, verify_integrity=verify_chain)
        expected_sha256: str | None = None
        commit_dict_id: str | None = None
        for commit in commits:
            if commit.capsule_uri == capsule_uri and commit.operation == "put":
                expected_sha256 = commit.capsule_sha256
                commit_dict_id = commit.compression_dict_id
                break
        if expected_sha256 is None:
            raise KeyError(
                f"Capsule '{capsule_uri}' not found in chain for "
                f"tenant={tenant!r} run_id={run_id!r}"
            )

        data = self._adapter.get_object(capsule_uri)
        observed = compute_sha256(data)
        if observed != expected_sha256:
            raise CapsuleIntegrityError(capsule_uri, expected_sha256, observed)

        # Decompress if the chain commit recorded a compression dictionary.
        if commit_dict_id is not None:
            if self._zstd is None:
                raise CompressionDictNotFound(commit_dict_id)
            data = self._zstd.decompress(data, commit_dict_id)

        return data

    def verify_capsule_chain(self, tenant: str, run_id: str, capsule_uri: str) -> bytes:
        """Fetch capsule bytes with full hash-chain verification (convenience wrapper).

        Equivalent to ``get_capsule(tenant, run_id, capsule_uri, verify_chain=True)``.
        Walks the entire ``prev_commit_hash`` chain for (tenant, run_id) before
        returning the capsule bytes.

        Note: As of G-A6, ``get_capsule()`` defaults to ``verify_chain=True``, so
        this method is now equivalent to a plain ``get_capsule()`` call.  It is
        retained for backward compatibility and to serve as a self-documenting
        alias that makes the caller's intent explicit.

        Args:
            tenant:      Tenant identifier.
            run_id:      Run identifier.
            capsule_uri: URI of the capsule object to fetch.

        Returns:
            Capsule bytes (same as ``get_capsule()``).

        Raises:
            KeyError: if capsule_uri has no committed 'put' entry in the chain.
            CapsuleIntegrityError: if fetched bytes SHA-256 != chain pin.
            ChainIntegrityError: if the ``prev_commit_hash`` chain is broken.
        """
        return self.get_capsule(tenant, run_id, capsule_uri, verify_chain=True)

    # -----------------------------------------------------------------------
    # list_capsules() — only committed (chain-visible) capsules
    # -----------------------------------------------------------------------

    def list_capsules(self, tenant: str, run_id: str) -> list[str]:
        """Return URIs of all committed capsules for (tenant, run_id).

        Only capsules with a chain commit are returned — partially-uploaded
        capsules (killed between steps 2-4) are invisible (FR-06).
        """
        commits = self._chain.read_chain(tenant, run_id)
        return [c.capsule_uri for c in commits if c.operation == "put"]

    # -----------------------------------------------------------------------
    # drain_wal() — idempotent WAL drainer
    # -----------------------------------------------------------------------

    def drain_wal(self) -> int:
        """Attempt to drain all pending WAL entries.

        For each undrained entry, re-attempts NovaSeal signing and chain commit.
        Entries that fail are left undrained for the next drain cycle.

        Returns:
            Number of entries successfully drained.
        """
        drained = 0
        for row_id, entry in self._wal.pending_entries():
            try:
                leaf = self._novaseal.sign(entry.capsule_sha256)
                novaseal_payload = json.dumps({
                    "capsule_sha256": entry.capsule_sha256,
                    "leaf_hash": leaf.leaf_hash,
                    "signed_at": leaf.signed_at,
                    "dsse_envelope": leaf.dsse_envelope_bytes.decode("utf-8"),
                }, separators=(",", ":")).encode("utf-8")
                novaseal_sha256 = compute_sha256(novaseal_payload)
                novaseal_key = derive_novaseal_key(entry.tenant, entry.capsule_sha256)
                if not self._adapter.object_exists(novaseal_key):
                    self._adapter.put_object(
                        key=novaseal_key,
                        data=novaseal_payload,
                        sha256_hex=novaseal_sha256,
                        retention_days=self._retention_days,
                        content_type="application/json",
                    )
                self._chain.append(
                    tenant=entry.tenant,
                    run_id=entry.run_id,
                    capsule_uri=entry.capsule_uri,
                    capsule_sha256=entry.capsule_sha256,
                    novaseal_leaf_hash=leaf.leaf_hash,
                    signed_at=leaf.signed_at,
                )
                self._wal.mark_drained(row_id)
                drained += 1
            except NovaSealUnavailable:
                log.debug("WAL drain: NovaSeal still unavailable for row_id=%d", row_id)
            except Exception as exc:
                log.error("WAL drain: unexpected error for row_id=%d: %s", row_id, exc)
        return drained


# ---------------------------------------------------------------------------
# OTel helpers (no-op if opentelemetry not installed)
# ---------------------------------------------------------------------------

def _otel_start_span(name: str, context: Any | None) -> Any:
    try:
        from opentelemetry import trace

        tracer = trace.get_tracer("novafabric.object_capsule_store")
        if context is not None:
            return tracer.start_span(name, context=context)
        return tracer.start_span(name)
    except ImportError:
        return None


def _otel_end_span(span: Any) -> None:
    if span is not None:
        try:
            span.end()
        except Exception:
            pass
