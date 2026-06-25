"""Safety-case template registry (ADR-0095, slice C0).

A template is a CAE *skeleton*: a tree of claims and argument strategies whose leaf
claims declare which ``evidence_kind``s satisfy them (``binding_spec.accepts``). The
compiler fills the leaves from real bundle artifacts and assigns each claim a backing
state mechanically.

Templates are YAML under ``safetycase/templates/``. ``clymer-generic-v0`` is fully
populated; ``eu-ai-act-annex-iv-v0`` and ``nist-ai-rmf-v0`` are stubs that load and
compile to a minimal honest tree (noted in their headers and in the ADR).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from novafabric.safetycase.models import (
    ClaimKind,
    InferenceType,
    ResidualRiskBasis,
)

_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

#: the three template ids recognised by the schema (kept in sync with the enum
#: in schemas/safety-case.schema.json).
KNOWN_TEMPLATE_IDS: tuple[str, ...] = (
    "clymer-generic-v0",
    "eu-ai-act-annex-iv-v0",
    "nist-ai-rmf-v0",
)


class TemplateError(ValueError):
    """Raised when a template id is unknown or a template file is malformed."""


class BindingSpec(BaseModel):
    """Which evidence kinds satisfy a leaf claim."""

    model_config = ConfigDict(extra="forbid")

    accepts: list[str] = Field(min_length=1)


class StrategySpec(BaseModel):
    """The argument strategy joining a claim to its children/evidence."""

    model_config = ConfigDict(extra="forbid")

    id: str
    strategy: str
    inference_type: InferenceType
    premises: list[str] = Field(default_factory=list)
    defeaters: list[str] = Field(default_factory=list)


class ClaimSpec(BaseModel):
    """A claim node in the skeleton; a leaf carries a ``binding_spec``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    statement: str
    claim_kind: ClaimKind
    strategy: StrategySpec | None = None
    children: list["ClaimSpec"] = Field(default_factory=list)
    binding_spec: BindingSpec | None = None

    @property
    def is_leaf(self) -> bool:
        return not self.children


class ResidualRiskSpec(BaseModel):
    """Template-declared residual-risk default (never a measured number)."""

    model_config = ConfigDict(extra="forbid")

    basis: ResidualRiskBasis = ResidualRiskBasis.NOT_QUANTIFIED
    caveat: str = ""


class Template(BaseModel):
    """A loaded CAE skeleton."""

    model_config = ConfigDict(extra="forbid")

    template_id: str
    title: str
    top_claim_id: str
    residual_risk: ResidualRiskSpec | None = None
    claims: list[ClaimSpec] = Field(min_length=1)

    @property
    def root(self) -> ClaimSpec:
        for claim in self.claims:
            if claim.id == self.top_claim_id:
                return claim
        raise TemplateError(
            f"top_claim_id {self.top_claim_id!r} not found in template "
            f"{self.template_id!r}"
        )


def load_template(template_id: str) -> Template:
    """Load and validate a template by id."""
    if template_id not in KNOWN_TEMPLATE_IDS:
        raise TemplateError(
            f"Unknown template {template_id!r}; known: {', '.join(KNOWN_TEMPLATE_IDS)}"
        )
    path = _TEMPLATE_DIR / f"{template_id}.yaml"
    if not path.exists():
        raise TemplateError(f"Template file missing for {template_id!r}: {path}")
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise TemplateError(f"Malformed template {template_id!r}: {exc}") from exc
    if not isinstance(raw, dict):
        raise TemplateError(f"Template {template_id!r} is not a mapping")
    try:
        tpl = Template.model_validate(raw)
    except ValueError as exc:
        raise TemplateError(f"Invalid template {template_id!r}: {exc}") from exc
    if tpl.template_id != template_id:
        raise TemplateError(
            f"Template id mismatch: file says {tpl.template_id!r}, "
            f"requested {template_id!r}"
        )
    return tpl


def _walk(claim: ClaimSpec) -> list[ClaimSpec]:
    out: list[ClaimSpec] = [claim]
    for child in claim.children:
        out.extend(_walk(child))
    return out


def iter_claims(template: Template) -> list[ClaimSpec]:
    """All claim specs in the template (depth-first, root first)."""
    return _walk(template.root)


def iter_leaf_claims(template: Template) -> list[ClaimSpec]:
    """The leaf claims (those with no children) — each must declare a binding_spec."""
    return [c for c in iter_claims(template) if c.is_leaf]


ClaimSpec.model_rebuild()
