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
"""Tenancy posture for the ``nova serve`` read path — ADR-0229, first slice.

ADR-0228 answers *what may I do*. This module answers *to whose data*. Neither
substitutes for the other, and `serve` had **neither**: `grep -rn "tenant"` over
`src/novafabric/serve/` returned exactly one line, a literal ``tenant="default"``.

The hard part is not the scoping call. It is that **the dashboard does not read
one store.** It reads seven, and they do not share a tenancy model. A design that
pretended one ``begin_tenant_context()`` scoped the dashboard would be wrong in a
way that looks right in every single-tenant test — the failure mode ADR-0229
names in its own Context, and one this repository keeps producing.

So each store declares a class (D1), the declaration is **asserted against the
store's actual schema** rather than against itself, and a deployment that cannot
be scoped is refused rather than silently served.

## What the measurement found, and where the ADR was wrong

ADR-0229's Context says of the seven stores that *"only the last of these has a
tenancy model at all"*. Re-derived 2026-09-06, that **undercounts by three**:

* ``cost.clickhouse_store`` carries a real ``tenant_id`` column, in the table DDL
  and in the ``ORDER BY`` key.
* ``object_capsule_store`` puts the tenant in the object key itself —
  ``capsules/<tenant>/<sha256[0:4]>/<sha256>/data.zst`` — and
  ``trust.tenant_keys.tenant_from_object_key`` reads it back.
* ``evidence_fabric.duckdb_accumulator`` carries a ``tenant`` column, which the
  ADR does credit elsewhere but not in that sentence.

The same measurement produced a **near-miss in the other direction**, and it is
the reason :data:`STORE_TENANCY` records *evidence* rather than a verdict:
``object_capsule_store`` mentions "tenant" fourteen times, and thirteen of those
are ADR-0243 per-tenant **KEKs** — tenancy of *key material*, not of query
results. Grepping for the word would have classified it ``aware`` for entirely
the wrong reason. A store is ``aware`` when it can answer *"show me only tenant
X's rows"*, and nothing else counts.

## Why this slice refuses at startup instead of per endpoint

ADR-0229 D2 specifies a **503 per endpoint** whose only backing store is
tenant-unsafe. Implemented literally today, that would return 503 for the 18
``/api/kg/*`` and 9 ``/api/lineage*`` routes — and go on serving ``/api/runs``,
the dashboard's highest-traffic surface, from ``registry.runs_cache``, which is
**also unsafe**. The operator would see some panels honestly refuse and conclude
the rest were scoped. That is the precise illusion D2 exists to prevent.

So the refusal is raised to the deployment: multi-tenant mode is **opt-in and
refused while any read-path store is unsafe**, naming them. Per-endpoint 503
becomes correct — and ships — once ``runs_cache`` is resolved (ADR-0229 OQ-1).
This is a deviation from D2 as written, recorded in the ADR's implementation
status rather than left for a reader to discover.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, TypedDict

__all__ = [
    "ENV_TENANCY_MODE",
    "StorePosture",
    "TenancyPosture",
    "STORE_TENANCY",
    "MultiTenancyUnavailableError",
    "StoreTenancy",
    "TenancyClass",
    "TenancyMode",
    "assert_multi_tenant_ready",
    "blocking_stores",
    "select_tenant",
    "tenancy_mode",
    "tenancy_posture",
]

#: Opt-in switch. Absent or ``single`` keeps today's behaviour exactly.
ENV_TENANCY_MODE: Final[str] = "NOVAFABRIC_SERVE_TENANCY"


class TenancyMode(str, enum.Enum):
    single = "single"
    multi = "multi"


class TenancyClass(str, enum.Enum):
    """What a store can be asked about tenancy.

    Deliberately three values, not a boolean. ``agnostic`` and ``unsafe`` both
    mean "does not filter by tenant" and have opposite safety consequences: one
    is isolated by the deployment, the other is a cross-tenant read waiting to
    happen. Collapsing them is how the second gets mistaken for the first.
    """

    aware = "aware"
    agnostic = "agnostic"
    unsafe = "unsafe"


@dataclass(frozen=True)
class StoreTenancy:
    """One store's declared tenancy, plus the evidence a test can re-check.

    ``evidence`` is not documentation. ``tests/serve/test_tenancy_registry.py``
    reads the named module and asserts the claim holds — an ``aware`` store must
    really carry its discriminator, and an ``unsafe`` store must really carry
    none. ADR-0229's own Consequences name declaration drift as the risk, so a
    test that only read this table back would be vacuous.
    """

    module: str
    tenancy: TenancyClass
    #: The literal token that carries the tenant in the store's schema or key
    #: format. ``None`` for agnostic/unsafe stores, which have none by definition.
    discriminator: str | None
    evidence: str
    #: Machine-readable reason, surfaced to operators. Required for ``unsafe``.
    reason: str | None = None


STORE_TENANCY: Final[dict[str, StoreTenancy]] = {
    "metadata_store": StoreTenancy(
        module="novafabric.metadata_store.rls",
        tenancy=TenancyClass.aware,
        discriminator="TENANT_SCOPED_TABLES",
        evidence=(
            "rls.py declares TENANT_SCOPED_TABLES and the canonical policy qual; "
            "MetadataStore.begin_tenant_context() is on the abstract interface"
        ),
    ),
    "evidence_fabric": StoreTenancy(
        module="novafabric.evidence_fabric.duckdb_accumulator",
        tenancy=TenancyClass.aware,
        discriminator="tenant",
        evidence="capsule_events DDL carries a `tenant TEXT NOT NULL` column",
    ),
    "cost_store": StoreTenancy(
        module="novafabric.cost.clickhouse_store",
        tenancy=TenancyClass.aware,
        discriminator="tenant_id",
        evidence="cost_events DDL carries tenant_id and orders by it",
    ),
    "object_capsule_store": StoreTenancy(
        module="novafabric.object_capsule_store.cas",
        tenancy=TenancyClass.aware,
        discriminator="capsules/{tenant}",
        evidence=(
            "the object key is capsules/<tenant>/<sha256[0:4]>/<sha256>/data.zst, "
            "read back by trust.tenant_keys.tenant_from_object_key. NOT the "
            "ADR-0243 per-tenant KEKs, which are key material and scope nothing"
        ),
    ),
    "capsule_dir": StoreTenancy(
        module="novafabric.serve.capsule_loader",
        tenancy=TenancyClass.agnostic,
        discriminator=None,
        evidence=(
            "a filesystem directory supplied by --capsule-dir; isolation is the "
            "deployment's, not the store's"
        ),
    ),
    "runs_cache": StoreTenancy(
        module="novafabric.registry.runs_cache",
        tenancy=TenancyClass.unsafe,
        discriminator=None,
        evidence="no tenant column anywhere in the runs-index schema",
        reason=(
            "the runs index has no tenant column, so /api/runs and every list "
            "built on it would return rows from every tenant (ADR-0229 OQ-1)"
        ),
    ),
    "knowledge_graph": StoreTenancy(
        module="novafabric.kg.store",
        tenancy=TenancyClass.unsafe,
        discriminator=None,
        evidence="KuzuDB node/edge schema carries no tenant property",
        reason=(
            "KuzuDB has no RLS analogue and the graph schema carries no tenant "
            "property; a tenancy model for it needs its own ADR (ADR-0229 OQ-2)"
        ),
    ),
    "lineage_store": StoreTenancy(
        module="novafabric.lineage._store",
        tenancy=TenancyClass.unsafe,
        discriminator=None,
        evidence="lineage_nodes and lineage_edges DDL carry no tenant column",
        reason=(
            "the SQLite lineage schema carries no tenant column, so provenance "
            "and blast-radius answers would span tenants"
        ),
    ),
}


class MultiTenancyUnavailableError(RuntimeError):
    """Raised when multi-tenant mode is requested and cannot be honoured.

    Carries the blocking stores so the message an operator sees names what to
    fix, rather than asserting a posture they cannot act on.
    """

    def __init__(self, blocking: Iterable[str]) -> None:
        self.blocking = tuple(blocking)
        details = "\n".join(
            f"  - {name}: {STORE_TENANCY[name].reason}" for name in self.blocking
        )
        super().__init__(
            f"{ENV_TENANCY_MODE}=multi was requested, but {len(self.blocking)} store(s) "
            f"in the serve read path cannot answer a tenant-scoped question:\n{details}\n"
            "Serving anyway would return one tenant's evidence to another. "
            "Unset the variable to run single-tenant, or use `nova server` "
            "(multi-user REST API) which enforces tenancy at the metadata store."
        )


def tenancy_mode(env: dict[str, str] | None = None) -> TenancyMode:
    """Resolve the configured mode. Anything but ``multi`` is ``single``.

    Unknown values fall back to ``single`` rather than raising: this is read at
    startup, and the safe reading of a typo is the restrictive mode that is also
    today's behaviour.
    """
    raw = (env or dict(os.environ)).get(ENV_TENANCY_MODE, "").strip().lower()
    return TenancyMode.multi if raw == TenancyMode.multi.value else TenancyMode.single


def blocking_stores() -> tuple[str, ...]:
    """Read-path stores that cannot answer a tenant-scoped question, sorted."""
    return tuple(
        sorted(
            name
            for name, decl in STORE_TENANCY.items()
            if decl.tenancy is TenancyClass.unsafe
        )
    )


def assert_multi_tenant_ready(env: dict[str, str] | None = None) -> None:
    """Refuse to run multi-tenant while any read-path store is unsafe (D2).

    A no-op in single-tenant mode, which is the default and the only mode a
    laptop ever sees.
    """
    if tenancy_mode(env) is not TenancyMode.multi:
        return
    blocking = blocking_stores()
    if blocking:
        raise MultiTenancyUnavailableError(blocking)


def select_tenant(requested: str | None, authorized: frozenset[str] | None) -> str | None:
    """Narrow within a credential's tenant set — never widen it (D4).

    *authorized* is the set carried by the ADR-0228 credential; ``None`` means
    the credential is not tenant-bound, which is the single-tenant default.

    The distinction this function exists to hold is the whole security boundary:
    a request may **select** among tenants it was already granted, and may never
    **specify** the set. An attacker-supplied ``?tenant=`` is not an access
    control mechanism, so a selector outside the authorized set is refused —
    not silently ignored, which would leave the caller believing they had
    narrowed when they had not.
    """
    if authorized is None:
        return requested
    if not authorized:
        raise PermissionError("credential carries an empty tenant set; no tenant is selectable")
    if requested is None:
        return next(iter(sorted(authorized))) if len(authorized) == 1 else None
    if requested not in authorized:
        raise PermissionError(
            f"tenant {requested!r} is not in this credential's authorized set; "
            "a selector may narrow within the set, never widen it"
        )
    return requested


class StorePosture(TypedDict):
    tenancy: str
    discriminator: str | None
    reason: str | None


class TenancyPosture(TypedDict):
    """Typed so callers can index it without narrowing.

    The first version returned ``dict[str, object]`` and mypy rejected
    ``", ".join(posture["blocking"])`` at the one call site — correctly: an
    untyped bag pushes the type error to every consumer instead of fixing it
    once at the source.
    """

    mode: str
    multi_tenant_ready: bool
    stores: dict[str, StorePosture]
    blocking: list[str]


def tenancy_posture(env: dict[str, str] | None = None) -> TenancyPosture:
    """Machine-readable posture, for `/api/doctor` and operator tooling.

    Reported in **both** modes on purpose. In single-tenant mode the unsafe
    stores are harmless and invisible; publishing them anyway is what turns
    "the KG has no tenancy model" from an unknown into something an operator
    can read before they try to deploy multi-tenant.
    """
    mode = tenancy_mode(env)
    return TenancyPosture(
        mode=mode.value,
        multi_tenant_ready=not blocking_stores(),
        stores={
            name: StorePosture(
                tenancy=decl.tenancy.value,
                discriminator=decl.discriminator,
                reason=decl.reason,
            )
            for name, decl in sorted(STORE_TENANCY.items())
        },
        blocking=list(blocking_stores()),
    )
