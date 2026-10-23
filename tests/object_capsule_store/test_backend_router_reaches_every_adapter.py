"""L13 — every shipped WORM adapter is reachable through ``make_adapter``.

``worm/gcs.py`` (``GcsWormAdapter``, per-object retention in LOCKED mode, with its
own ``worm-gcs`` extra and test suite) existed, but ``backend_router._BACKENDS``
and ``make_adapter`` had no ``gcs`` tag, so nothing that selects a backend by
name — ``NOVA_OCS_BACKEND``, ``nova rebuild-metadata-db --backend``, backup /
restore — could reach it. The guard below fails for the NEXT adapter module that
lands without a router entry, not just this one.
"""

from __future__ import annotations

import pkgutil
from unittest.mock import MagicMock

import pytest

import novafabric.object_capsule_store.worm as worm_pkg
from novafabric.object_capsule_store import backend_router
from novafabric.object_capsule_store.backend_router import make_adapter
from novafabric.object_capsule_store.worm.gcs import GcsWormAdapter

#: Adapter module -> router tag. ``base`` is the ABC, not an adapter.
_MODULE_TO_TAG = {
    "s3": "s3",
    "minio": "minio",
    "ceph": "ceph_rgw",
    "azure": "azure_blob",
    "gcs": "gcs",
}


def test_every_adapter_module_has_a_router_tag() -> None:
    modules = {m.name for m in pkgutil.iter_modules(worm_pkg.__path__)} - {"base"}
    unmapped = modules - set(_MODULE_TO_TAG)
    assert not unmapped, f"new WORM adapter module(s) without a router tag: {unmapped}"
    missing = {tag for tag in _MODULE_TO_TAG.values() if tag not in backend_router._BACKENDS}
    assert not missing, f"adapter tag(s) unreachable through make_adapter: {missing}"


def test_gcs_tag_builds_the_gcs_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NOVA_OBJECT_STORE_ENCRYPTION", "NOVA_OBJECT_STORE_KEK_PATH"):
        monkeypatch.delenv(var, raising=False)
    client = MagicMock()
    adapter = make_adapter("gcs", bucket_name="capsules", client=client)
    assert isinstance(adapter, GcsWormAdapter)
    assert adapter._bucket_name == "capsules"


def test_unknown_tag_lists_gcs_among_the_choices() -> None:
    with pytest.raises(ValueError, match="gcs"):
        make_adapter("nope")
