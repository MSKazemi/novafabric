"""Jurisdiction routing (ADR-0247 slice 1).

The properties, one test each: placement follows the tenant→jurisdiction
map; an unroutable jurisdiction fails closed at write time (never lands
elsewhere — the load-bearing rule); unmapped tenants keep today's behavior
via the default pool; the ADR-0077 residency gate finally has a consumer —
cross-border reads deny by default and pass with a directional grant; the
config loader validates its shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.compliance.sovereignty import ResidencyPolicy
from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.jurisdiction_router import (
    JurisdictionRoutingAdapter,
    JurisdictionRoutingError,
    ResidencyReadDenied,
    load_jurisdiction_config,
)


def _put(adapter, key: str, payload: bytes) -> None:
    adapter.put_object(key, payload, compute_sha256(payload), retention_days=1)


@pytest.fixture()
def region_setup() -> tuple[JurisdictionRoutingAdapter, InMemoryWormAdapter, InMemoryWormAdapter, InMemoryWormAdapter]:
    eu = InMemoryWormAdapter()
    us = InMemoryWormAdapter()
    default = InMemoryWormAdapter()
    router = JurisdictionRoutingAdapter(
        routes={"EU": eu, "US": us},
        tenant_jurisdictions={"acme": "EU", "globex": "US", "initech": "MARS"},
        default=default,
        residency_policy=ResidencyPolicy(allow_cross_read={"EU": ["CH"]}),
    )
    return router, eu, us, default


def test_writes_land_in_their_region(region_setup) -> None:
    router, eu, us, default = region_setup
    _put(router, "capsules/acme/aa/s1/data.zst", b"eu-bytes")
    _put(router, "capsules/globex/bb/s2/data.zst", b"us-bytes")
    _put(router, "capsules/unmapped/cc/s3/data.zst", b"default-bytes")

    assert eu.object_exists("capsules/acme/aa/s1/data.zst")
    assert not us.object_exists("capsules/acme/aa/s1/data.zst")
    assert us.object_exists("capsules/globex/bb/s2/data.zst")
    assert default.object_exists("capsules/unmapped/cc/s3/data.zst")

    # Reads resolve through the same map.
    assert router.get_object("capsules/acme/aa/s1/data.zst") == b"eu-bytes"
    assert router.get_object("capsules/globex/bb/s2/data.zst") == b"us-bytes"


def test_unroutable_jurisdiction_fails_closed(region_setup) -> None:
    router, *_ = region_setup
    # initech maps to MARS, for which no backend exists: the write must be
    # refused, never silently placed elsewhere.
    with pytest.raises(JurisdictionRoutingError, match="MARS"):
        _put(router, "capsules/initech/dd/s4/data.zst", b"lost-bytes")


def test_no_default_makes_unmapped_fail_closed() -> None:
    router = JurisdictionRoutingAdapter(
        routes={"EU": InMemoryWormAdapter()},
        tenant_jurisdictions={},
        default=None,
    )
    with pytest.raises(JurisdictionRoutingError, match="no default"):
        _put(router, "capsules/anyone/aa/s/data.zst", b"x")


def test_residency_gate_denies_by_default_and_honors_grants(region_setup) -> None:
    router, *_ = region_setup
    _put(router, "capsules/acme/aa/s1/data.zst", b"eu-bytes")

    # Same jurisdiction: always allowed.
    assert router.get_object_checked("capsules/acme/aa/s1/data.zst", "EU") == b"eu-bytes"
    # Granted direction (EU data readable from CH).
    assert router.get_object_checked("capsules/acme/aa/s1/data.zst", "CH") == b"eu-bytes"
    # Ungranted direction: denied, with the reason carried.
    with pytest.raises(ResidencyReadDenied, match="denied"):
        router.get_object_checked("capsules/acme/aa/s1/data.zst", "US")


def test_apply_identical_refuses_cross_region_pairs(region_setup) -> None:
    router, *_ = region_setup
    payload = b"pair"
    with pytest.raises(JurisdictionRoutingError, match="one region"):
        router.apply_identical(
            "capsules/acme/aa/s1/data.zst", payload, compute_sha256(payload),
            "capsules/globex/bb/s2/data.zst", payload, compute_sha256(payload),
            retention_days=1,
        )


def test_config_loader_roundtrip(tmp_path: Path) -> None:
    cfg = tmp_path / "jurisdictions.yaml"
    cfg.write_text(
        "tenants:\n  acme: EU\n  globex: US\nallow_cross_read:\n  EU: [CH]\n"
    )
    tenants, policy = load_jurisdiction_config(cfg)
    assert tenants == {"acme": "EU", "globex": "US"}
    assert policy.allow_cross_read == {"EU": ["CH"]}


def test_config_loader_rejects_bad_shape(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("tenants:\n  acme: [not, a, tag]\n")
    with pytest.raises(ValueError, match="tenant"):
        load_jurisdiction_config(cfg)
