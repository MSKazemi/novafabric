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

"""Emitter output conforms to the vendored in-toto/SLSA schemas (NF-030/031, ADR-0096)."""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.envelopes.intoto import capsule_statement
from novafabric.envelopes.schema import (
    EnvelopeSchemaError,
    validate_intoto_statement,
    validate_slsa_provenance,
)
from novafabric.envelopes.slsa import promotion_provenance

_SHA = "a" * 64


def _capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "capsule"
    cap.mkdir()
    (cap / "capsule.yaml").write_text("run_id: 01HXAY7M5JZ8R7K4P9DPBYK2WX\n")
    (cap / "trace.jsonl").write_text('{"e": 1}\n')
    return cap


def test_capsule_statement_validates_as_intoto(tmp_path: Path) -> None:
    validate_intoto_statement(capsule_statement(_capsule(tmp_path)))


def test_promotion_provenance_validates_as_slsa() -> None:
    stmt = promotion_provenance(
        asset_ref="agent:x@1.0.0",
        asset_sha256=_SHA,
        eval_container="sha256:abc",
        seeds=[1],
        datasets=[{"uri": "dataset:gaia@2026-05", "sha256": "deadbeef"}],
        gate="regression-gate/v1",
        invocation_id="01HXBM1Y3K2NGH9V0RD9P0ZDC4",
    )
    validate_slsa_provenance(stmt)  # validates outer Statement + inner SLSA predicate


def test_minimal_provenance_validates() -> None:
    validate_slsa_provenance(promotion_provenance(asset_ref="a@1", asset_sha256=_SHA))


def test_missing_predicate_type_fails_intoto() -> None:
    bad = {"_type": "https://in-toto.io/Statement/v1", "subject": [{"digest": {"sha256": _SHA}}]}
    with pytest.raises(EnvelopeSchemaError, match="predicateType"):
        validate_intoto_statement(bad)


def test_slsa_missing_build_definition_fails() -> None:
    bad = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"digest": {"sha256": _SHA}}],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {"runDetails": {"builder": {"id": "https://b"}}},
    }
    with pytest.raises(EnvelopeSchemaError, match="buildDefinition"):
        validate_slsa_provenance(bad)


def test_slsa_builder_without_id_fails() -> None:
    bad = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"digest": {"sha256": _SHA}}],
        "predicateType": "https://slsa.dev/provenance/v1",
        "predicate": {
            "buildDefinition": {"buildType": "https://bt", "externalParameters": {}},
            "runDetails": {"builder": {}},
        },
    }
    with pytest.raises(EnvelopeSchemaError):
        validate_slsa_provenance(bad)
