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
"""ADR-0101 §NF-021 — span-level claim grounding audit (structural).

Over a capsule's span tree, a model/generation span is a *claim* and a tool/retrieval
span is *evidence*. A claim is ``grounded`` when supporting evidence precedes it on the
answer path, ``ungrounded`` otherwise. Deterministic and structural — no NLP; an
ungrounded claim is a hallucination-risk finding, not a semantic judgment.
"""

from __future__ import annotations

import json
from pathlib import Path

from novafabric.diagnose.claim_audit import (
    ClaimGrounding,
    audit_claims,
)


def _capsule(tmp_path: Path, models: list[dict], tools: list[dict]) -> Path:
    cap = tmp_path / "cap"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: run-1\nstatus: completed\n", encoding="utf-8")
    if models:
        (cap / "model-calls.jsonl").write_text(
            "\n".join(json.dumps(m) for m in models) + "\n", encoding="utf-8"
        )
    if tools:
        (cap / "tool-calls.jsonl").write_text(
            "\n".join(json.dumps(t) for t in tools) + "\n", encoding="utf-8"
        )
    return cap


_MODELS = [
    {"call_id": "m0", "model": "gpt", "started_at": "2026-07-01T00:00:00Z"},  # before any tool
    {"call_id": "m2", "model": "gpt", "started_at": "2026-07-01T00:00:02Z"},  # after the search
]
_TOOLS = [
    {"call_id": "s1", "tool_name": "search", "started_at": "2026-07-01T00:00:01Z"},
]


class TestGrounding:
    def test_claim_after_evidence_is_grounded(self, tmp_path: Path) -> None:
        audit = audit_claims(_capsule(tmp_path, _MODELS, _TOOLS))
        m2 = next(c for c in audit.claims if c.step_id == "m2")
        assert m2.grounding is ClaimGrounding.grounded
        assert "s1" in m2.supporting_evidence_ids

    def test_claim_before_any_evidence_is_ungrounded(self, tmp_path: Path) -> None:
        audit = audit_claims(_capsule(tmp_path, _MODELS, _TOOLS))
        m0 = next(c for c in audit.claims if c.step_id == "m0")
        assert m0.grounding is ClaimGrounding.ungrounded
        assert m0.supporting_evidence_ids == []

    def test_ungrounded_count(self, tmp_path: Path) -> None:
        audit = audit_claims(_capsule(tmp_path, _MODELS, _TOOLS))
        assert audit.n_claims == 2
        assert audit.n_ungrounded == 1


class TestEdgeCases:
    def test_no_model_spans_yields_no_claims(self, tmp_path: Path) -> None:
        audit = audit_claims(_capsule(tmp_path, [], _TOOLS))
        assert audit.claims == []
        assert audit.n_claims == 0

    def test_all_claims_ungrounded_when_no_tools(self, tmp_path: Path) -> None:
        audit = audit_claims(_capsule(tmp_path, _MODELS, []))
        assert audit.n_claims == 2
        assert audit.n_ungrounded == 2
        assert all(c.grounding is ClaimGrounding.ungrounded for c in audit.claims)

    def test_as_dict_round_trips(self, tmp_path: Path) -> None:
        audit = audit_claims(_capsule(tmp_path, _MODELS, _TOOLS))
        d = audit.as_dict()
        assert d["run_id"] == "run-1"
        assert d["n_claims"] == 2
        assert d["n_ungrounded"] == 1
        assert {c["step_id"] for c in d["claims"]} == {"m0", "m2"}

    def test_ungrounded_finding_is_labelled_a_risk_not_a_verdict(self, tmp_path: Path) -> None:
        audit = audit_claims(_capsule(tmp_path, _MODELS, _TOOLS))
        m0 = next(c for c in audit.claims if c.step_id == "m0")
        # Honest: a structural grounding signal, not a semantic truth judgment.
        assert "risk" in m0.rationale.lower() or "no supporting evidence" in m0.rationale.lower()
