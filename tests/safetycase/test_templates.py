"""ADR-0095 slice C0 — template registry: load, validate, enumerate leaves."""

from __future__ import annotations

import pytest

from novafabric.safetycase.templates import (
    KNOWN_TEMPLATE_IDS,
    TemplateError,
    iter_leaf_claims,
    load_template,
)


class TestLoad:
    def test_known_ids(self) -> None:
        assert KNOWN_TEMPLATE_IDS == (
            "clymer-generic-v0",
            "eu-ai-act-annex-iv-v0",
            "nist-ai-rmf-v0",
        )

    @pytest.mark.parametrize("template_id", KNOWN_TEMPLATE_IDS)
    def test_each_template_loads(self, template_id: str) -> None:
        tpl = load_template(template_id)
        assert tpl.template_id == template_id
        assert tpl.top_claim_id == tpl.root.id
        assert tpl.root.statement

    def test_unknown_id_raises(self) -> None:
        with pytest.raises(TemplateError, match="Unknown template"):
            load_template("no-such-template-v9")


class TestStructure:
    def test_clymer_has_three_leaf_claims(self) -> None:
        tpl = load_template("clymer-generic-v0")
        leaves = list(iter_leaf_claims(tpl))
        assert len(leaves) == 3
        # every leaf declares accepted evidence kinds
        for leaf in leaves:
            assert leaf.binding_spec is not None
            assert leaf.binding_spec.accepts

    def test_clymer_accepts_eval_and_replay(self) -> None:
        tpl = load_template("clymer-generic-v0")
        accepted: set[str] = set()
        for leaf in iter_leaf_claims(tpl):
            assert leaf.binding_spec is not None
            accepted.update(leaf.binding_spec.accepts)
        assert "replay_attestation" in accepted
        assert "judge_aggregate" in accepted
        assert "completeness_assertion" in accepted

    @pytest.mark.parametrize(
        "template_id", ["eu-ai-act-annex-iv-v0", "nist-ai-rmf-v0"]
    )
    def test_stub_templates_have_a_leaf(self, template_id: str) -> None:
        tpl = load_template(template_id)
        leaves = list(iter_leaf_claims(tpl))
        assert leaves, "even a stub must produce at least one leaf claim"

    def test_root_is_not_a_leaf_when_it_has_children(self) -> None:
        tpl = load_template("clymer-generic-v0")
        leaf_ids = {leaf.id for leaf in iter_leaf_claims(tpl)}
        assert tpl.root.id not in leaf_ids
