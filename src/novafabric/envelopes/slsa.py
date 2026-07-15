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

"""SLSA v1 provenance for a promotion (NF-031, ADR-0096).

Emits a ``https://slsa.dev/provenance/v1`` predicate (as an in-toto Statement) capturing
the sealed eval closure that justified a promotion: the ``buildDefinition`` losslessly
records the eval container digest, dataset hashes and seeds; ``runDetails`` records the
promotion decision and the gate. It validates against the upstream SLSA v1 schema and is
signed as the DSSE payload via :func:`novafabric.evidence.intoto.dsse_sign`.

Additive: emitting provenance never replaces the existing promotion record (requirement 7).
"""

from __future__ import annotations

from typing import Any, Sequence

from novafabric.evidence.intoto import INTOTO_STATEMENT_TYPE

SLSA_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
PROMOTE_BUILD_TYPE = "https://novafabric.dev/promote/v1"
#: SLSA-for-ML profile build type (NF-057).
PROMOTE_ML_BUILD_TYPE = "https://novafabric.dev/promote-ml/v1"
DEFAULT_BUILDER_ID = "https://novafabric.dev/runner/local"


def promotion_provenance(
    *,
    asset_ref: str,
    asset_sha256: str,
    eval_container: str | None = None,
    seeds: Sequence[int] | None = None,
    datasets: Sequence[dict[str, str]] | None = None,
    builder_id: str = DEFAULT_BUILDER_ID,
    invocation_id: str | None = None,
    decision: str = "promoted",
    gate: str | None = None,
    build_type: str = PROMOTE_BUILD_TYPE,
    gate_byproduct_name: str = "promotion-gate",
    eval_verdict_sha256: str | None = None,
) -> dict[str, Any]:
    """Build a SLSA v1 provenance Statement for a successful promotion (NF-031/NF-057).

    ``datasets`` is a sequence of ``{"uri": ..., "sha256": ...}`` resolved dependencies.
    ``asset_sha256`` / dataset ``sha256`` accept an optional ``sha256:`` prefix.

    The defaults emit the generic NF-031 promotion provenance. Passing
    ``build_type=PROMOTE_ML_BUILD_TYPE`` + ``eval_verdict_sha256`` (see
    :func:`ml_promotion_provenance`) emits the SLSA-for-ML profile (NF-057), which adds an
    ``eval-verdict`` byproduct carrying the digest of the gating eval verdict.
    """
    internal: dict[str, Any] = {}
    if eval_container is not None:
        internal["evalContainer"] = eval_container
    if seeds is not None:
        internal["seeds"] = list(seeds)

    resolved: list[dict[str, Any]] = []
    for dep in datasets or []:
        resolved.append(
            {"uri": dep["uri"], "digest": {"sha256": dep["sha256"].removeprefix("sha256:")}}
        )

    build_definition: dict[str, Any] = {
        "buildType": build_type,
        "externalParameters": {"asset": asset_ref},
        "internalParameters": internal,
        "resolvedDependencies": resolved,
    }

    byproducts: list[dict[str, Any]] = [{"name": "promotion-decision", "content": decision}]
    if gate is not None:
        byproducts.append({"name": gate_byproduct_name, "content": gate})
    if eval_verdict_sha256 is not None:
        verdict_digest = eval_verdict_sha256.removeprefix("sha256:")
        byproducts.append({"name": "eval-verdict", "digest": {"sha256": verdict_digest}})

    metadata: dict[str, Any] = {}
    if invocation_id is not None:
        metadata["invocationId"] = invocation_id

    run_details: dict[str, Any] = {
        "builder": {"id": builder_id},
        "metadata": metadata,
        "byproducts": byproducts,
    }

    return {
        "_type": INTOTO_STATEMENT_TYPE,
        "subject": [
            {"name": asset_ref, "digest": {"sha256": asset_sha256.removeprefix("sha256:")}}
        ],
        "predicateType": SLSA_PREDICATE_TYPE,
        "predicate": {"buildDefinition": build_definition, "runDetails": run_details},
    }


def ml_promotion_provenance(
    *,
    asset_ref: str,
    asset_sha256: str,
    eval_verdict_sha256: str,
    eval_container: str | None = None,
    seeds: Sequence[int] | None = None,
    datasets: Sequence[dict[str, str]] | None = None,
    builder_id: str = DEFAULT_BUILDER_ID,
    invocation_id: str | None = None,
    decision: str = "promoted",
    gate: str | None = None,
) -> dict[str, Any]:
    """Build the SLSA-for-ML profile provenance for a model promotion (NF-057).

    Same shape as :func:`promotion_provenance` but with the ``promote-ml/v1`` build type,
    a ``gate-rule`` byproduct, and a mandatory ``eval-verdict`` digest byproduct capturing
    the gating eval verdict — so a consumer can bind the promoted model to the exact eval
    result that justified it.
    """
    return promotion_provenance(
        asset_ref=asset_ref,
        asset_sha256=asset_sha256,
        eval_container=eval_container,
        seeds=seeds,
        datasets=datasets,
        builder_id=builder_id,
        invocation_id=invocation_id,
        decision=decision,
        gate=gate,
        build_type=PROMOTE_ML_BUILD_TYPE,
        gate_byproduct_name="gate-rule",
        eval_verdict_sha256=eval_verdict_sha256,
    )
