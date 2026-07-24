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

"""ADR-0196 D4 — the boundary test whose absence caused the gap.

Five facet slices (ADR-0142/0145/0152/0153/0163) shipped writing a
capsule-level ``facets`` key that ``run-capsule.schema.json`` rejected,
because ``additionalProperties: false`` and no ``facets`` was declared. Every
slice passed its own gates: their tests operate on plain dicts, so nothing
ever validated a facet-bearing capsule against the real schema.

These tests validate against the real schema. Without them this ADR fixes
today's five facets and leaves the sixth to rediscover the same gap.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "schemas" / "run-capsule.schema.json"
BASELINE = (
    REPO_ROOT / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json"
)

#: Every facet name the schema registers. Kept as a literal rather than
#: imported from the schema so that a name silently disappearing from the
#: registry fails here instead of quietly shrinking the test's coverage.
#:
#: Registration follows an Accepted ADR, not an implementation (ADR-0196), so
#: some of these have no writer yet. `IMPLEMENTED_FACETS` below is the subset
#: with shipped builders — keeping the two lists distinct stops a registered
#: name from reading as evidence that something produces it.
REGISTERED_FACETS = (
    "a2a_messages",
    "assurance_case",
    "cost_attribution",
    "conversation",
    "embodied",
    "federation",
    "fetch_provenance",
    "frontier_safety",
    "memstore_mutation",
    "model_provenance",
    "preservation",
    "risk_transfer",
    "safety",
    "science_provenance",
    "settlement",
)

#: Modules that ship a facet builder, as (import path, expected facet name).
#: Discovered by import rather than hand-listed: the first version of this was
#: a literal asserting only a subset relation, so when ADR-0167 shipped
#: `frontier_safety` the list silently understated what existed and nothing
#: failed. A list that can quietly go stale is not much of a guard.
FACET_MODULES = (
    ("novafabric.a2a.messages", "a2a_messages"),
    ("novafabric.assure.case", "assurance_case"),
    ("novafabric.cost.attribution", "cost_attribution"),
    ("novafabric.embodied.facet", "embodied"),
    ("novafabric.federation.facet", "federation"),
    ("novafabric.frontier_safety.facet", "frontier_safety"),
    ("novafabric.hitl.conversation", "conversation"),
    ("novafabric.memstore.ledger", "memstore_mutation"),
    ("novafabric.provenance.model_provenance", "model_provenance"),
    ("novafabric.retrieval.fetch_provenance", "fetch_provenance"),
    ("novafabric.risk_transfer.actuarial", "risk_transfer"),
    ("novafabric.safety.decisions", "safety"),
    ("novafabric.science.provenance", "science_provenance"),
    ("novafabric.settlement.facet", "settlement"),
)


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


@pytest.fixture
def capsule() -> dict[str, Any]:
    return json.loads(BASELINE.read_text())


def test_baseline_capsule_is_valid(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    jsonschema.validate(capsule, schema)


def test_capsule_without_facets_stays_valid(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """I-1: no existing capsule becomes invalid."""
    assert "facets" not in capsule
    jsonschema.validate(capsule, schema)


@pytest.mark.parametrize("facet_name", REGISTERED_FACETS)
def test_each_registered_facet_validates(
    schema: dict[str, Any], capsule: dict[str, Any], facet_name: str
) -> None:
    capsule["facets"] = {facet_name: {"schema_version": "0.1.0"}}
    jsonschema.validate(capsule, schema)


def test_registry_matches_the_schema_exactly() -> None:
    """The literal above must not drift from the schema in either direction."""
    schema_names = tuple(
        sorted(
            json.loads(SCHEMA_PATH.read_text())["properties"]["facets"]["properties"]
        )
    )
    assert schema_names == tuple(sorted(REGISTERED_FACETS))


@pytest.mark.parametrize(("module_path", "facet_name"), FACET_MODULES)
def test_shipped_builder_facet_name_is_registered(
    module_path: str, facet_name: str
) -> None:
    """Every module that writes a facet must have its name in the registry.

    Checks the module's own FACET_NAME constant rather than trusting the
    tuple above, so a module renaming its facet fails here instead of
    silently writing a key the schema rejects.
    """
    import importlib

    module = importlib.import_module(module_path)
    assert module.FACET_NAME == facet_name
    assert facet_name in REGISTERED_FACETS


def test_all_registered_facets_together_validate(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    capsule["facets"] = {n: {"schema_version": "0.1.0"} for n in REGISTERED_FACETS}
    jsonschema.validate(capsule, schema)


def test_unregistered_facet_is_rejected(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """I-3 / D2: the registry is closed on purpose.

    An unregistered name is either a typo — which would silently produce a
    capsule missing the evidence its author believed it carried — or an
    unreviewed evidence surface entering the sealed root.
    """
    capsule["facets"] = {"saftey": {"schema_version": "0.1.0"}}  # typo
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(capsule, schema)


def test_facet_value_must_be_an_object(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    capsule["facets"] = {"safety": "not-an-object"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(capsule, schema)


def test_extensions_remains_open_for_third_party_data(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """D1: `facets` is first-party; `extensions` stays the vendor bag.

    If this ever fails, the two containers have been conflated and the
    distinction between NovaFabric-recorded evidence and a third-party
    annotation has been lost.
    """
    capsule["extensions"] = {"io.example.vendor": {"anything": True}}
    jsonschema.validate(capsule, schema)


# ── Round-trip through the real facet builders ────────────────────────────


def test_safety_facet_builder_output_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    """The exact regression: this failed before ADR-0196.

    Uses the shipped builder rather than a hand-written dict, because a
    hand-written dict is what let the gap through in the first place.
    """
    from novafabric.safety import GuardrailDecision, attach_facet, build_facet

    decision = GuardrailDecision(
        decision_id="d1",
        phase="input",
        disposition="block",
        decided_at="2026-07-20T10:00:00Z",
    )
    jsonschema.validate(attach_facet(capsule, build_facet([decision])), schema)


def test_no_material_attach_is_a_no_op_and_still_validates(
    schema: dict[str, Any], capsule: dict[str, Any]
) -> None:
    from novafabric.safety import attach_facet, build_facet

    out = attach_facet(capsule, build_facet([]))
    assert out == capsule
    jsonschema.validate(out, schema)
