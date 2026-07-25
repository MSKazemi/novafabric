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
"""ADR-0101 §NF-019/§NF-022 — No-LLM causal-graph back-trace attribution.

Reconstructs the span parent/child causal graph from a capsule's recorded spans and
back-traces the *root* failure nodes — a failing node whose whole ancestor chain is
error-free is a causal root; a failing node downstream of another failure is not.
Deterministic, no LLM. Every candidate is verification-gated ``unverified`` (NF-022):
no intervention replay was run, so it is a ranked candidate, never a proven root cause.
"""

from __future__ import annotations

import json
from pathlib import Path

from novafabric.diagnose.causal_graph import (
    CausalAttribution,
    causal_root_candidates,
)


def _capsule(tmp_path: Path, spans: list[dict]) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: run-1\nstatus: failed\n", encoding="utf-8")
    (cap / "trace.jsonl").write_text(
        "\n".join(json.dumps(s) for s in spans) + "\n", encoding="utf-8"
    )
    return cap


# A → B(err) → C(err), and A → D(err). Roots: B and D. C is downstream of B's failure.
_SPANS = [
    {"span_id": "a", "name": "root", "started_at": "2026-07-01T00:00:00Z"},
    {"span_id": "b", "parent_span_id": "a", "name": "planner", "status": "error",
     "error": "plan step failed", "started_at": "2026-07-01T00:00:01Z"},
    {"span_id": "c", "parent_span_id": "b", "name": "executor", "status": "error",
     "error": "downstream of planner", "started_at": "2026-07-01T00:00:02Z"},
    {"span_id": "d", "parent_span_id": "a", "name": "retriever", "status": "error",
     "error": "independent retrieval failure", "started_at": "2026-07-01T00:00:03Z"},
]


class TestBackTrace:
    def test_roots_are_failing_nodes_with_no_failing_ancestor(self, tmp_path: Path) -> None:
        attr = causal_root_candidates(_capsule(tmp_path, _SPANS))
        roots = {c.step_id for c in attr.root_candidates}
        assert roots == {"b", "d"}

    def test_downstream_failure_is_not_a_root(self, tmp_path: Path) -> None:
        attr = causal_root_candidates(_capsule(tmp_path, _SPANS))
        assert "c" not in {c.step_id for c in attr.root_candidates}

    def test_counts_all_error_nodes(self, tmp_path: Path) -> None:
        attr = causal_root_candidates(_capsule(tmp_path, _SPANS))
        assert attr.n_error_nodes == 3  # b, c, d


class TestVerificationGate:
    def test_every_candidate_is_unverified(self, tmp_path: Path) -> None:
        attr = causal_root_candidates(_capsule(tmp_path, _SPANS))
        assert attr.root_candidates
        for c in attr.root_candidates:
            assert c.verification == "unverified"
            # NF-022: never presented as proven — the note says so.
            assert "not a proven root cause" in c.rationale.lower() or "candidate" in c.rationale.lower()

    def test_candidates_carry_taxonomy_and_deterministic_rank(self, tmp_path: Path) -> None:
        a1 = causal_root_candidates(_capsule(tmp_path / "x", _SPANS))
        a2 = causal_root_candidates(_capsule(tmp_path / "y", _SPANS))
        # Deterministic ordering + scores across runs.
        assert [c.step_id for c in a1.root_candidates] == [c.step_id for c in a2.root_candidates]
        assert all(c.taxonomy is not None for c in a1.root_candidates)


class TestEdgeCases:
    def test_no_errors_yields_no_roots(self, tmp_path: Path) -> None:
        spans = [{"span_id": "a", "name": "ok", "started_at": "2026-07-01T00:00:00Z"}]
        attr = causal_root_candidates(_capsule(tmp_path, spans))
        assert attr.root_candidates == []
        assert attr.n_error_nodes == 0

    def test_orphan_error_span_is_a_root(self, tmp_path: Path) -> None:
        # A failing span whose parent is absent from the trace is treated as a root.
        spans = [
            {"span_id": "z", "parent_span_id": "missing", "name": "orphan",
             "status": "error", "error": "boom", "started_at": "2026-07-01T00:00:00Z"},
        ]
        attr = causal_root_candidates(_capsule(tmp_path, spans))
        assert {c.step_id for c in attr.root_candidates} == {"z"}

    def test_as_dict_round_trips(self, tmp_path: Path) -> None:
        attr = causal_root_candidates(_capsule(tmp_path, _SPANS))
        d = attr.as_dict()
        assert d["run_id"] == "run-1"
        assert d["n_error_nodes"] == 3
        assert all(rc["verification"] == "unverified" for rc in d["root_candidates"])
        assert isinstance(attr, CausalAttribution)
