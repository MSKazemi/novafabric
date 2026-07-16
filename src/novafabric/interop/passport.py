"""Portable agent-passport projection (ADR-0149 / NF-179).

A pure projection — like the AIBOM / PROV-JSON exporters — that gathers the identity,
lineage, AIBOM, eval-card, package, and delegation references NovaFabric already produces
for an agent into a single portable **passport document**, verifiable offline as
``green`` / ``amber`` / ``red``:

* ``green`` — every passport component is present and resolvable (carried as a ref),
* ``amber`` — the identity anchor exists but a component is absent, or a component is
  **opaque** (present but unattestable, e.g. an opaque lineage ancestor),
* ``red`` — the identity anchor is absent, so there is no basis for a passport.

The passport **MUST NOT** claim ancestry NovaFabric cannot attest: an opaque ancestor is
reported ``amber``, never dressed up as ``green``. Each component carries a ref/digest only,
never the component body.

This first slice emits the **unsigned** projection (``signed=False``); binding it through
the shipped seal path into a signed, portable ``agent-passport.json`` is a documented
follow-on. The collector that reads these refs from a sealed capsule is likewise a follow-on
— this slice takes the refs as input.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum

from pydantic import BaseModel

#: The passport components NovaFabric projects, in a stable render order.
PASSPORT_COMPONENTS: tuple[str, ...] = (
    "identity",
    "lineage",
    "aibom",
    "card",
    "package",
    "delegation",
)

#: The anchor component: without it there is no basis for a passport.
_ANCHOR = "identity"


class ComponentState(str, Enum):
    present = "present"
    absent = "absent"
    opaque = "opaque"


class PassportStatus(str, Enum):
    green = "green"
    amber = "amber"
    red = "red"


class PassportComponent(BaseModel):
    name: str
    state: ComponentState
    ref: str | None = None  # digest/ref when present — never the component body


class PassportDocument(BaseModel):
    agent_ref: str
    components: list[PassportComponent]
    status: PassportStatus
    signed: bool = False  # unsigned projection; seal-path signing is a follow-on
    # Intentionally NO valid/trusted/certified/verdict field beyond the honest status.


def build_passport(
    *,
    agent_ref: str,
    present: Mapping[str, str] = {},
    opaque: Sequence[str] = (),
) -> PassportDocument:
    """Project the passport components NovaFabric can attest into one portable document.

    ``present`` maps a passport-component name to its ref/digest; ``opaque`` names
    components that exist but cannot be attested (e.g. an opaque lineage ancestor). A
    component in neither is ``absent``. The status is ``red`` when the identity anchor is
    absent, ``green`` when every component is present, else ``amber`` — the honest verdict
    for a missing or opaque component. Never claims ancestry NovaFabric cannot attest.
    """
    opaque_set = set(opaque)
    unknown = (set(present) | opaque_set) - set(PASSPORT_COMPONENTS)
    if unknown:
        raise ValueError(f"unknown passport component(s): {', '.join(sorted(unknown))}")
    both = set(present) & opaque_set
    if both:
        raise ValueError(
            f"component(s) both present and opaque: {', '.join(sorted(both))}"
        )

    components: list[PassportComponent] = []
    for name in PASSPORT_COMPONENTS:
        if name in opaque_set:
            state, ref = ComponentState.opaque, None
        elif name in present:
            state, ref = ComponentState.present, present[name]
        else:
            state, ref = ComponentState.absent, None
        components.append(PassportComponent(name=name, state=state, ref=ref))

    by_name = {c.name: c for c in components}
    if by_name[_ANCHOR].state is ComponentState.absent:
        status = PassportStatus.red
    elif all(c.state is ComponentState.present for c in components):
        status = PassportStatus.green
    else:
        status = PassportStatus.amber

    return PassportDocument(agent_ref=agent_ref, components=components, status=status)
