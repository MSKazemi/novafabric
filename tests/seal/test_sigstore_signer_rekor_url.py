"""``SigstoreSigner(rekor_url=...)`` is refused, not silently ignored (review trust-evidence-18).

The parameter was stored and never read: signing and verification always used
the public Sigstore instance (``ClientTrustConfig.production()``), so an
operator pointing at a private Rekor sent their signing request to the public
one with no warning. The sigstore SDK has no Rekor-URL override to pass it to.
"""
from __future__ import annotations

import pytest

from novafabric.trust.novaseal.sigstore_signer import SigstoreSigner


def test_a_custom_rekor_url_is_refused() -> None:
    with pytest.raises(ValueError, match="public Sigstore"):
        SigstoreSigner(rekor_url="https://rekor.internal.example")


def test_the_default_signer_still_constructs() -> None:
    SigstoreSigner()
    SigstoreSigner(rekor_url=None)
