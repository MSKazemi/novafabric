"""ADR-0095 slice C3 — the fleshed-out EU AI Act Annex IV + NIST AI RMF templates.

These templates were stubs; they now carry one sub-claim per Annex IV element /
per RMF function. The tests pin that they still load + compile cleanly and that the
Annex IV template binds to the same 15 element ids as the compliance mapping in
``src/novafabric/compliance/export/annex_iv_mapping.yaml``.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from novafabric.compliance.export.safety_case import render_safety_case
from novafabric.safetycase.compiler import SafetyCaseCompiler
from novafabric.safetycase.models import BackingState, Claim
from novafabric.safetycase.templates import iter_leaf_claims, load_template

_ANNEX_IV_MAPPING = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "novafabric"
    / "compliance"
    / "export"
    / "annex_iv_mapping.yaml"
)


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "01HXAY7M5JZ8R7K4P9DPBYK2WX"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    return cap


def _mapping_element_ids() -> set[int]:
    data = yaml.safe_load(_ANNEX_IV_MAPPING.read_text())
    return {int(e["element_id"]) for e in data["elements"]}


class TestAnnexIVTemplate:
    def test_loads(self) -> None:
        tpl = load_template("eu-ai-act-annex-iv-v0")
        assert tpl.template_id == "eu-ai-act-annex-iv-v0"

    def test_has_one_leaf_per_annex_iv_element(self) -> None:
        tpl = load_template("eu-ai-act-annex-iv-v0")
        leaves = iter_leaf_claims(tpl)
        # leaf ids are C-annex-iv-<n>; extract the element numbers
        leaf_nums = {
            int(c.id.rsplit("-", 1)[1]) for c in leaves if c.id.startswith("C-annex-iv-")
        }
        assert leaf_nums == _mapping_element_ids()
        assert len(leaf_nums) == 15

    def test_every_leaf_declares_a_binding(self) -> None:
        tpl = load_template("eu-ai-act-annex-iv-v0")
        for leaf in iter_leaf_claims(tpl):
            assert leaf.binding_spec is not None
            assert leaf.binding_spec.accepts

    def test_compiles_to_unsupported_root_with_no_artifacts(
        self, tmp_path: Path
    ) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "eu-ai-act-annex-iv-v0")
        root = case.get_claim(case.top_claim_id)
        assert root is not None
        # no artifacts resolve -> honest UNSUPPORTED, never falsely SUPPORTED
        assert root.backing_state is BackingState.UNSUPPORTED
        # 15 element sub-claims + root = 16 claim nodes
        claims = [n for n in case.nodes if isinstance(n, Claim)]
        assert len(claims) == 16

    def test_compiled_case_renders_to_annex_iv(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "eu-ai-act-annex-iv-v0")
        out = render_safety_case(case, "annex-iv")
        assert "Annex IV" in out
        assert "UNSUPPORTED" in out
        assert "compliant" not in out.lower()


class TestNistTemplate:
    def test_loads(self) -> None:
        tpl = load_template("nist-ai-rmf-v0")
        assert tpl.template_id == "nist-ai-rmf-v0"

    def test_has_four_function_leaves(self) -> None:
        tpl = load_template("nist-ai-rmf-v0")
        leaf_ids = {c.id for c in iter_leaf_claims(tpl)}
        assert leaf_ids == {"C-GOVERN", "C-MAP", "C-MEASURE", "C-MANAGE"}

    def test_compiles_and_renders_grouped(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "nist-ai-rmf-v0")
        out = render_safety_case(case, "nist-rmf")
        for fn in ("GOVERN", "MAP", "MEASURE", "MANAGE"):
            assert fn in out

    def test_compiled_case_json_roundtrips(self, tmp_path: Path) -> None:
        cap = _capsule(tmp_path)
        case = SafetyCaseCompiler().build(cap, "nist-ai-rmf-v0")
        out = render_safety_case(case, "json")
        assert json.loads(out)["template_id"] == "nist-ai-rmf-v0"
