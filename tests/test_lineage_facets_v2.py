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
"""ADR-0109 NF-061/062 — row + transformation lineage facets (the verifiable half).

The load-bearing invariant is **I-2: names/keys/hashes, never values**. A row
facet stores hashes of row keys, and a transform facet stores a content-address of
the operation — never a raw key, cell value, or the operation's contents. The
builders are fail-open (I-3: return None, never raise) and round-trip through the
existing `LineageEdge.facets` field (I-4).
"""

from __future__ import annotations

import pytest

from novafabric.lineage._facets_v2 import (
    ROW_FACET_KEY,
    TRANSFORM_FACET_KEY,
    build_row_facet,
    build_transform_facet,
)
from novafabric.lineage._types import LineageEdge


class TestTransformFacet:
    def test_content_addresses_the_operation(self) -> None:
        f1 = build_transform_facet("df.groupby('x').sum()", op_kind="pandas")
        f2 = build_transform_facet("df.groupby('x').sum()", op_kind="pandas")
        assert f1 is not None and f2 is not None
        assert f1.op_digest == f2.op_digest  # same operation → same digest
        assert f1.op_digest.startswith("sha256:")
        assert f1.op_kind == "pandas"

    def test_different_operations_differ(self) -> None:
        a = build_transform_facet("a", op_kind="py")
        b = build_transform_facet("b", op_kind="py")
        assert a is not None and b is not None and a.op_digest != b.op_digest

    def test_never_stores_operation_contents(self) -> None:
        secret_op = "SELECT ssn FROM patients WHERE name = 'Alice'"
        f = build_transform_facet(secret_op, op_kind="sql")
        assert f is not None
        blob = f.model_dump_json()
        assert "ssn" not in blob and "Alice" not in blob and "patients" not in blob

    def test_fail_open_on_bad_input(self) -> None:
        assert build_transform_facet(None) is None  # type: ignore[arg-type]
        assert build_transform_facet("") is None


class TestRowFacet:
    def test_hashes_row_keys_never_raw(self) -> None:
        f = build_row_facet(["user-42", "user-99"])
        assert f is not None
        blob = f.model_dump_json()
        assert "user-42" not in blob and "user-99" not in blob
        assert len(f.row_key_hashes) == 2
        assert all(h.startswith("sha256:") for h in f.row_key_hashes)

    def test_same_key_same_hash(self) -> None:
        a = build_row_facet(["k"])
        b = build_row_facet(["k"])
        assert a is not None and b is not None
        assert a.row_key_hashes == b.row_key_hashes

    def test_cap_truncates_and_downgrades(self) -> None:
        f = build_row_facet([f"k{i}" for i in range(1000)])
        assert f is not None
        assert len(f.row_key_hashes) <= 256
        assert f.truncated is True
        assert f.confidence == "heuristic"

    def test_small_set_is_exact(self) -> None:
        f = build_row_facet(["a", "b"])
        assert f is not None and f.truncated is False and f.confidence == "exact"

    def test_fail_open(self) -> None:
        assert build_row_facet([]) is None
        assert build_row_facet(None) is None  # type: ignore[arg-type]


class TestRoundTrip:
    def test_facets_attach_and_survive_as_dict(self) -> None:
        transform = build_transform_facet("op", op_kind="py")
        rows = build_row_facet(["k1"])
        assert transform is not None and rows is not None
        edge = LineageEdge(
            edge_type="derived",
            source={"kind": "run", "run_id": "01RUNA"},
            target={"kind": "asset", "asset_ref": "m@1", "registry": "local"},
            confidence="high",
            capsule_run_id="01RUNA",
            facets={
                TRANSFORM_FACET_KEY: transform.model_dump(),
                ROW_FACET_KEY: rows.model_dump(),
            },
        )
        d = edge.as_dict()
        assert d["facets"][TRANSFORM_FACET_KEY]["op_digest"] == transform.op_digest
        assert d["facets"][ROW_FACET_KEY]["row_key_hashes"] == rows.row_key_hashes


@pytest.mark.parametrize("garbage", [123, {"x": 1}, object(), b"bytes-not-str"])
def test_transform_never_raises_on_garbage(garbage: object) -> None:
    # I-3: fail-open on anything, never raise.
    assert build_transform_facet(garbage) is None  # type: ignore[arg-type]
