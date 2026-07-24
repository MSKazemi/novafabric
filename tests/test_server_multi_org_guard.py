"""ADR-0178: refuse to serve multiple orgs from one shared capsule store.

The capsule store is not tenant-partitioned — `GET /capsules` lists every
org's capsules to any authenticated reader, and the tenant-scoped metadata
write is best-effort and inert on the shipped auth path. Rather than let that
be discovered in production, a deployment that has actually created a second
organization must acknowledge the limitation.

This narrows a claim; it does not fix the isolation gap (that remediation is
Security-Architect gated). Single-org deployments — the default — are
unaffected, which these tests pin explicitly.
"""

from __future__ import annotations

import pytest

from novafabric.server.config import (
    ServerConfig,
    UnpartitionedStoreError,
    check_multi_org_shared_store,
)


def _config(**kwargs: object) -> ServerConfig:
    return ServerConfig(**kwargs)  # type: ignore[arg-type]


def test_single_org_starts_normally() -> None:
    """The default shape must never be blocked."""
    check_multi_org_shared_store(_config(), org_count=1)


def test_zero_orgs_starts_normally() -> None:
    """Pre-bootstrap / empty registry is not a multi-tenant deployment."""
    check_multi_org_shared_store(_config(), org_count=0)


def test_multiple_orgs_refused_without_acknowledgement() -> None:
    with pytest.raises(UnpartitionedStoreError) as exc:
        check_multi_org_shared_store(_config(), org_count=2)
    message = str(exc.value)
    # The error must say what is wrong, not just that something is.
    assert "not tenant-partitioned" in message.lower()
    assert "ADR-0178" in message
    # ...and point at the analysis rather than leaving the operator guessing.
    assert "security-reviews" in message
    # ...and name the escape hatch.
    assert "--i-accept-shared-capsule-store" in message


def test_multiple_orgs_allowed_once_acknowledged() -> None:
    check_multi_org_shared_store(
        _config(i_accept_shared_capsule_store=True), org_count=5
    )


def test_acknowledgement_defaults_to_off() -> None:
    """Secure by default: the operator must opt in, never opt out."""
    assert _config().i_accept_shared_capsule_store is False


def test_acknowledgement_readable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_SERVER_I_ACCEPT_SHARED_CAPSULE_STORE", "1")
    assert ServerConfig().i_accept_shared_capsule_store is True


def test_error_subclasses_valueerror_like_the_insecure_bind_guard() -> None:
    """Matches the ADR-0184 precedent so pydantic surfaces it the same way."""
    assert issubclass(UnpartitionedStoreError, ValueError)
