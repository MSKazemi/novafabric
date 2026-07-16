# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
"""Tests for WormAdapter.iter_objects streaming listing (ADR-0175)."""
from __future__ import annotations

import types

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter
from novafabric.object_capsule_store.worm.base import WormAdapter


def _seed(adapter: InMemoryWormAdapter, keys: list[str]) -> None:
    for k in keys:
        adapter.put_log_object(k, b"x")


def test_iter_objects_yields_same_set_as_list_objects() -> None:
    adapter = InMemoryWormAdapter()
    _seed(adapter, ["a/1", "a/2", "b/3", "c/4"])
    assert sorted(adapter.iter_objects("a/")) == ["a/1", "a/2"]
    # The streamed set (order-independent) matches list_objects exactly.
    assert set(adapter.iter_objects("")) == set(adapter.list_objects(""))


def test_iter_objects_returns_a_lazy_generator() -> None:
    adapter = InMemoryWormAdapter()
    _seed(adapter, ["p/1"])
    gen = adapter.iter_objects("p/")
    assert isinstance(gen, types.GeneratorType)


def test_iter_objects_empty_prefix_match() -> None:
    adapter = InMemoryWormAdapter()
    _seed(adapter, ["x/1"])
    assert list(adapter.iter_objects("no-such/")) == []


def test_default_iter_objects_works_for_a_custom_adapter() -> None:
    # A minimal adapter that only implements list_objects must still get a
    # working iter_objects from the ABC default (backwards compatibility).
    class _Tiny(WormAdapter):
        def put_object(self, *a, **k):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def apply_identical(self, *a, **k):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        def get_object(self, key: str) -> bytes:
            raise NotImplementedError

        def put_log_object(self, key: str, data: bytes) -> str:
            raise NotImplementedError

        def put_log_object_if_absent(self, key: str, data: bytes) -> str:
            raise NotImplementedError

        def list_objects(self, prefix: str) -> list[str]:
            return ["z/1", "z/2"]

        def delete_object(self, key: str) -> None:
            raise NotImplementedError

        def object_exists(self, key: str) -> bool:
            raise NotImplementedError

    tiny = _Tiny()
    assert list(tiny.iter_objects("z/")) == ["z/1", "z/2"]
