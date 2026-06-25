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
"""Tests for BackendRouter and InMemoryWormAdapter."""

from __future__ import annotations

import pytest

from novafabric.object_capsule_store.backend_router import InMemoryWormAdapter, make_adapter
from novafabric.object_capsule_store.cas import compute_sha256
from novafabric.object_capsule_store.exceptions import CASMismatchError
from novafabric.object_capsule_store.worm.base import ConditionalPutConflict


def test_make_adapter_local():
    adapter = make_adapter("local")
    assert isinstance(adapter, InMemoryWormAdapter)


def test_make_adapter_unknown():
    with pytest.raises(ValueError, match="Unknown backend"):
        make_adapter("unknown_backend")


# ---------------------------------------------------------------------------
# InMemoryWormAdapter: put_object / get_object
# ---------------------------------------------------------------------------

def test_put_get_object():
    adapter = InMemoryWormAdapter()
    content = b"hello capsule"
    sha256 = compute_sha256(content)
    result = adapter.put_object("key1", content, sha256, 365)
    assert result.key == "key1"
    assert adapter.get_object("key1") == content


def test_put_object_cas_mismatch():
    """put_object raises CASMismatchError if sha256 doesn't match content."""
    adapter = InMemoryWormAdapter()
    content = b"real content"
    wrong_sha256 = "0" * 64
    with pytest.raises(CASMismatchError):
        adapter.put_object("key", content, wrong_sha256, 365)


def test_get_object_not_found():
    adapter = InMemoryWormAdapter()
    with pytest.raises(FileNotFoundError):
        adapter.get_object("missing_key")


# ---------------------------------------------------------------------------
# Conditional-PUT
# ---------------------------------------------------------------------------

def test_put_log_object_if_absent_success():
    adapter = InMemoryWormAdapter()
    adapter.put_log_object_if_absent("chain/key", b"data")
    assert adapter.get_object("chain/key") == b"data"


def test_put_log_object_if_absent_conflict():
    adapter = InMemoryWormAdapter()
    adapter.put_log_object_if_absent("chain/key", b"first")
    with pytest.raises(ConditionalPutConflict):
        adapter.put_log_object_if_absent("chain/key", b"second")
    # Original data unchanged
    assert adapter.get_object("chain/key") == b"first"


# ---------------------------------------------------------------------------
# apply_identical
# ---------------------------------------------------------------------------

def test_apply_identical_uploads_both():
    adapter = InMemoryWormAdapter()
    data_a = b"capsule bytes"
    data_b = b"novaseal sibling"
    sha_a = compute_sha256(data_a)
    sha_b = compute_sha256(data_b)
    ra, rb = adapter.apply_identical("key_a", data_a, sha_a, "key_b", data_b, sha_b, 365)
    assert adapter.get_object("key_a") == data_a
    assert adapter.get_object("key_b") == data_b


# ---------------------------------------------------------------------------
# list_objects / object_exists / delete_object
# ---------------------------------------------------------------------------

def test_list_objects_prefix():
    adapter = InMemoryWormAdapter()
    adapter.put_log_object("prefix/a", b"1")
    adapter.put_log_object("prefix/b", b"2")
    adapter.put_log_object("other/c", b"3")
    listed = adapter.list_objects("prefix/")
    assert listed == ["prefix/a", "prefix/b"]


def test_object_exists():
    adapter = InMemoryWormAdapter()
    assert not adapter.object_exists("k")
    adapter.put_log_object("k", b"v")
    assert adapter.object_exists("k")


def test_delete_object():
    adapter = InMemoryWormAdapter()
    adapter.put_log_object("k", b"v")
    adapter.delete_object("k")
    assert not adapter.object_exists("k")
