"""Assurance-case assessor package + renewal delta (ADR-0166 D5, first slice).

A :class:`AssessorPackage` is the sealed, self-contained bundle an assessor re-walks **offline**:
the argument graph (D1), the bound capsule roots, the conformance map (D3), the currency ledger
(D2), the open defeaters (D4), and a coverage metric. It carries everything needed to re-verify the
argument and **nothing that decides the outcome** — there is no verdict field, mirroring the
D3 receipt principle ("assembled against", never "meets").

A :class:`RenewalDelta` records what moved since a prior sealed package (by digest) so a
re-assessment sees exactly the change: nodes added, evidence refreshed, defeaters opened/closed,
and conformance clauses revised.

This first slice is the **pure model + deterministic digest + delta computation**. It does not
seal: per the ADR, the assessor package reuses the existing Evidence-Bundle seal path (DSSE +
timestamp), which this slice deliberately leaves to a follow-up. It adds no capsule-schema field
and no new serialization format — it composes the D1–D4 models over a canonical JSON digest.
"""
from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

from novafabric.assure.case import AssuranceCase, NodeType
from novafabric.assure.conformance import ConformanceMap
from novafabric.assure.currency import CurrencyLedger
from novafabric.assure.defeater import Defeater, open_defeaters


class BoundCapsule(BaseModel):
    capsule_root: str  # the sealed capsule's Merkle root
    inclusion_proof: list[str] = []  # sibling hashes proving a leaf's inclusion (optional)


class AssessorPackage(BaseModel):
    package_id: str
    case: AssuranceCase
    bound_capsules: list[BoundCapsule] = []
    conformance_map: ConformanceMap = ConformanceMap()
    currency_ledger: CurrencyLedger = CurrencyLedger()
    open_defeaters: list[Defeater] = []
    coverage: float | None = None
    # Intentionally NO verdict field — the package carries evidence to re-walk, never a decision.


class RenewalDelta(BaseModel):
    prior_package_digest: str
    nodes_added: list[str] = []
    evidence_refreshed: list[str] = []  # node ids whose evidence digests changed
    defeaters_opened: list[str] = []
    defeaters_closed: list[str] = []
    clauses_revised: list[str] = []  # "<node_id>@<clause_id>" whose claim digest changed


def _solution_coverage(case: AssuranceCase) -> float | None:
    """Fraction of ``solution`` nodes that carry at least one evidence reference."""
    solutions = [n for n in case.nodes if n.type is NodeType.solution]
    if not solutions:
        return None
    supported = sum(1 for n in solutions if n.evidence_refs)
    return supported / len(solutions)


def build_assessor_package(
    *,
    package_id: str,
    case: AssuranceCase,
    bound_capsules: list[BoundCapsule] | tuple[BoundCapsule, ...] = (),
    conformance_map: ConformanceMap | None = None,
    currency_ledger: CurrencyLedger | None = None,
    defeaters: list[Defeater] | tuple[Defeater, ...] = (),
) -> AssessorPackage:
    """Assemble an :class:`AssessorPackage` from the D1–D4 components.

    Only *open* defeaters are carried (a cleared defeater no longer undermines the argument), and
    the coverage metric is derived from the case. No outcome is computed — the package is evidence
    for an assessor to re-walk, not a verdict.
    """
    return AssessorPackage(
        package_id=package_id,
        case=case,
        bound_capsules=list(bound_capsules),
        conformance_map=conformance_map or ConformanceMap(),
        currency_ledger=currency_ledger or CurrencyLedger(),
        open_defeaters=open_defeaters(list(defeaters)),
        coverage=_solution_coverage(case),
    )


def package_digest(package: AssessorPackage) -> str:
    """Deterministic SHA-256 over the package's canonical JSON (excluding ``package_id``).

    The digest binds the *content* an assessor re-walks; the ``package_id`` (a caller-chosen label)
    is excluded so two packages with identical evidence share a digest.
    """
    payload = package.model_dump(mode="json", exclude={"package_id"})
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _evidence_digests(case: AssuranceCase) -> dict[str, frozenset[str]]:
    return {n.id: frozenset(e.digest for e in n.evidence_refs) for n in case.nodes}


def _clause_digests(cmap: ConformanceMap) -> dict[str, str]:
    return {f"{e.node_id}@{e.clause_id}": e.claim_digest for e in cmap.entries}


def compute_renewal_delta(prior: AssessorPackage, current: AssessorPackage) -> RenewalDelta:
    """Diff two assessor packages into a :class:`RenewalDelta` (what moved, prior → current)."""
    prior_ids = {n.id for n in prior.case.nodes}
    nodes_added = sorted(n.id for n in current.case.nodes if n.id not in prior_ids)

    prior_ev, cur_ev = _evidence_digests(prior.case), _evidence_digests(current.case)
    evidence_refreshed = sorted(
        nid for nid, digs in cur_ev.items() if nid in prior_ev and prior_ev[nid] != digs
    )

    prior_open = {d.id for d in prior.open_defeaters}
    cur_open = {d.id for d in current.open_defeaters}
    defeaters_opened = sorted(cur_open - prior_open)
    defeaters_closed = sorted(prior_open - cur_open)

    prior_cl = _clause_digests(prior.conformance_map)
    cur_cl = _clause_digests(current.conformance_map)
    clauses_revised = sorted(
        key for key, dig in cur_cl.items() if key in prior_cl and prior_cl[key] != dig
    )

    return RenewalDelta(
        prior_package_digest=package_digest(prior),
        nodes_added=nodes_added,
        evidence_refreshed=evidence_refreshed,
        defeaters_opened=defeaters_opened,
        defeaters_closed=defeaters_closed,
        clauses_revised=clauses_revised,
    )
