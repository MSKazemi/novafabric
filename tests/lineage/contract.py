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

"""One behavioural contract for every `AbstractLineageStore` backend.

NovaFabric ships five lineage backends. Two of them — SQLite and Kuzu — are
embedded and run on a laptop with no container at all; Postgres, AGE and
JanusGraph need a daemon and therefore only run in CI. That asymmetry is the
whole problem this module exists to solve.

Before it, each backend was verified by its own hand-written test file, and the
files did not assert the same things. `tests/lineage/test_backends_kuzu.py`
asserted `len(result) >= 1` and set-membership where the SQLite and Postgres
tests asserted exact refs and exact order. So "the Kuzu tests pass" and "Kuzu
behaves like the reference" were unrelated statements, and two real divergences
lived in the gap (see `KNOWN_DIVERGENCES` below).

A contract fixes that asymmetry rather than papering over it: every backend runs
the *same* assertions, so a laptop run against the two embedded backends is
evidence about the interface, not merely about SQLite. What CI adds on top is
the backend-specific SQL/Cypher binding — not the semantics.

Usage from a backend's test module::

    from lineage.contract import CONTRACT_CHECKS, contract_params, load

    @pytest.mark.parametrize("check", contract_params())
    def test_lineage_contract(check, store):
        CONTRACT_CHECKS[check](store)

A backend that genuinely cannot satisfy a check declares it::

    contract_params({"provenance_reaches_assets": "no Asset node table (BL-xxx)"})

which marks that one check `xfail(strict=True)`. Strict is the point: the day
someone fixes the backend, the xfail becomes an unexpected pass and the suite
fails until the exemption is deleted. A known gap stays visible and expires by
itself, instead of decaying into a comment nobody re-reads.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import pytest

from novafabric.lineage._types import LineageEdge

#: The reference graph. A replay chain D -> C -> B -> A, plus A consuming an
#: asset — small enough to reason about, wide enough to cover both node kinds
#: and a depth bound.
_ASSET_REF = "model:foo@1.0.0"
_EXPECTED_ASSET_REF = f"local:{_ASSET_REF}"


def _run(run_id: str) -> dict[str, Any]:
    return {"kind": "run", "run_id": run_id}


def _asset(ref: str) -> dict[str, Any]:
    return {"kind": "asset", "asset_ref": ref, "registry": "local"}


def reference_edges() -> list[LineageEdge]:
    """The graph every contract check runs against."""
    return [
        LineageEdge(
            edge_type="consumed",
            source=_run("01RUNA"),
            target=_asset(_ASSET_REF),
            confidence="high",
            capsule_run_id="01RUNA",
        ),
        LineageEdge(
            edge_type="replayed_from",
            source=_run("01RUNB"),
            target=_run("01RUNA"),
            confidence="high",
            capsule_run_id="01RUNB",
        ),
        LineageEdge(
            edge_type="replayed_from",
            source=_run("01RUNC"),
            target=_run("01RUNB"),
            confidence="high",
            capsule_run_id="01RUNC",
        ),
        LineageEdge(
            edge_type="replayed_from",
            source=_run("01RUND"),
            target=_run("01RUNC"),
            confidence="high",
            capsule_run_id="01RUND",
        ),
    ]


def load(store: Any) -> None:
    """Insert the reference graph into *store*."""
    for edge in reference_edges():
        store.insert(edge)


def refs(rows: list[dict[str, Any]]) -> set[str]:
    return {row["ref"] for row in rows}


# ── the checks ────────────────────────────────────────────────────────────────
# Each is a single behavioural statement about AbstractLineageStore, written
# against absolute expected values rather than "same as SQLite". A differential
# assertion cannot run on a laptop that has only one backend, and it cannot say
# which of the two is wrong when they disagree.


def check_blast_radius(store: Any) -> None:
    """Everything downstream of A, to full depth."""
    assert refs(store.blast_radius("01RUNA", max_depth=5)) == {
        "01RUNB",
        "01RUNC",
        "01RUND",
    }


def check_blast_radius_depth_bound(store: Any) -> None:
    """`max_depth` truncates: one hop reaches B and stops."""
    assert refs(store.blast_radius("01RUNA", max_depth=1)) == {"01RUNB"}


def check_replay_chain_is_step_ordered(store: Any) -> None:
    """`replay_chain` is ordered by replay step, nearest ancestor first.

    Order is the contract, not an accident of the query plan: the chain is
    rendered to users as "D was replayed from C, which came from B…". A set
    would let a backend return the ancestors shuffled and still pass.
    """
    assert [row["ref"] for row in store.replay_chain("01RUND")] == [
        "01RUNC",
        "01RUNB",
        "01RUNA",
    ]


def check_provenance_reaches_assets(store: Any) -> None:
    """Provenance crosses the run -> asset edge and returns the asset's ref.

    An asset is a first-class lineage node. A backend that models runs only will
    still return *a* row here — an empty one — so this asserts the ref, which is
    the part a user reads.
    """
    assert refs(store.provenance("01RUNA", depth=5)) == {_EXPECTED_ASSET_REF}


def check_unknown_run_is_empty(store: Any) -> None:
    """An unknown run is empty everywhere, never an error."""
    assert store.provenance("nope", depth=5) == []
    assert store.blast_radius("nope", max_depth=5) == []
    assert store.replay_chain("nope") == []


def check_insert_is_idempotent(store: Any) -> None:
    """Re-inserting the same graph changes no answer."""
    before = refs(store.blast_radius("01RUNA", max_depth=5))
    load(store)
    assert refs(store.blast_radius("01RUNA", max_depth=5)) == before


CONTRACT_CHECKS: Mapping[str, Callable[[Any], None]] = {
    "blast_radius": check_blast_radius,
    "blast_radius_depth_bound": check_blast_radius_depth_bound,
    "replay_chain_is_step_ordered": check_replay_chain_is_step_ordered,
    "provenance_reaches_assets": check_provenance_reaches_assets,
    "unknown_run_is_empty": check_unknown_run_is_empty,
    "insert_is_idempotent": check_insert_is_idempotent,
}


@dataclass(frozen=True)
class Divergence:
    """Why a backend cannot pass one check.

    `strict` defaults to True, and should stay True: a strict xfail turns into a
    failure the moment the backend starts passing, so the exemption cannot
    outlive the defect quietly.

    Set it False *only* for a divergence that is non-deterministic, where the
    backend sometimes produces the right answer by luck. A strict xfail on a
    flaky result is itself flaky — it fails whenever the coin lands the right way
    up, which is a worse test than none. Record the measured rate in `reason` so
    the exemption stays a finding rather than a shrug.
    """

    reason: str
    strict: bool = True


def contract_params(
    known_divergences: Mapping[str, str | Divergence] | None = None,
) -> list[Any]:
    """Every check as a `pytest.param`, xfailing the declared divergences.

    *known_divergences* maps a check name to why this backend cannot pass it —
    either a plain string (strict) or a `Divergence`.
    """
    divergences = {
        name: value if isinstance(value, Divergence) else Divergence(value)
        for name, value in dict(known_divergences or {}).items()
    }
    unknown = sorted(set(divergences) - set(CONTRACT_CHECKS))
    if unknown:
        raise ValueError(
            f"known_divergences names checks that do not exist: {unknown}. "
            "A renamed check would otherwise silently stop being exempted — or "
            "silently stop being run."
        )
    return [
        pytest.param(
            name,
            id=name,
            marks=(
                [
                    pytest.mark.xfail(
                        strict=divergences[name].strict,
                        reason=divergences[name].reason,
                    )
                ]
                if name in divergences
                else []
            ),
        )
        for name in sorted(CONTRACT_CHECKS)
    ]
