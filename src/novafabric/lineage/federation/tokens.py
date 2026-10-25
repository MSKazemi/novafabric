"""Cross-site reference token verification (C-3) — **future design, fails closed**.

``novafabric.trust.novaseal`` exposes no ``verify`` function, so today
:func:`verify_cross_site_token` returns ``False`` for every token and nothing in the
tree calls it. It verifies nothing; it only refuses. A federation that trusts
cross-site references needs a real verifier, with its own design and tests, before
this can return ``True`` (review replay-lineage-registry-19).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def verify_cross_site_token(signature_ref: str, trust_root: bytes | None = None) -> bool:
    """Verify a NovaSeal cross-site reference token.

    Returns True if the token is valid (or if trust_root is None, delegates
    to NovaSeal's default verification). Returns False on any verification
    failure without raising.
    """
    try:
        from novafabric.trust import novaseal as _novaseal  # noqa: F401
    except ImportError:
        log.warning(
            "novafabric.trust.novaseal is not available; cross-site token verification skipped"
        )
        return False

    verify_fn = getattr(_novaseal, "verify", None)
    if verify_fn is None:
        log.warning(
            "novafabric.trust.novaseal does not expose a verify() function; "
            "cross-site token verification skipped"
        )
        return False

    try:
        if trust_root is not None:
            return bool(verify_fn(signature_ref, trust_root=trust_root))
        return bool(verify_fn(signature_ref))
    except Exception as exc:  # noqa: BLE001
        log.warning("Cross-site token verification failed: %s", exc)
        return False
