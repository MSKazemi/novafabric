"""Local, user-extensible model-pricing catalog (ADR-0133, model-pricing-catalog-v0).

A pricing catalog is a single local YAML or JSON file that assigns per-token
(or per-image) prices to model IDs, so NovaFabric can compute offline cost for
models absent from the built-in ``PRICE_TABLE`` — self-hosted, fine-tuned, or
private models — without editing source and without any network call.

Invariants (normative — see ``the private design/spec/model-pricing-catalog-v0.md``):

- **Fully local and offline.** The merged catalog is a pure function of the
  on-disk discovery layers plus the built-in table. No remote registry, no
  price fetch, no network behavior of any kind.
- **Merge, most-specific wins.** Layers (lowest to highest): built-in
  ``PRICE_TABLE`` < user config (``~/.config/novafabric/pricing.yaml``) <
  project (``./.novafabric/pricing.yaml``) < explicit ``--pricing-catalog``.
  On a ``model_id`` collision the higher layer's entries fully replace the
  lower layer's (per-entry replacement, never per-field merge).
- **Unknown model = 0.0, exactly as today.** The catalog only makes *more*
  models costable; it never introduces a new failure for unmatched models.
- **Never fail the workload.** A malformed catalog layer is skipped with a
  warning; lower layers (ultimately the built-in table) still apply.
- **Recorded cost is never overwritten.** A recorded ``nova.cost`` block on a
  model-call record always wins verbatim (``basis="recorded"``); a
  catalog-derived figure is always labeled ``basis="estimated"``.
- **Reproducible.** The merged catalog is content-addressed (SHA-256 over its
  canonical JSON form, built-in table included) so a cost figure can be pinned
  to the exact pricing that produced it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

log = logging.getLogger(__name__)

#: Catalog file schema version (spec model-pricing-catalog-v0).
SCHEMA_VERSION = "0.1.0"

#: The closed set of priceable usage types (ties to the ADR-0132 taxonomy).
PRICING_USAGE_TYPES: tuple[str, ...] = (
    "input",
    "output",
    "cached",
    "reasoning",
    "audio",
    "image",
)

#: Discovery layer names, lowest to highest precedence.
LAYER_ORDER: tuple[str, ...] = ("builtin", "user", "project", "explicit")

PriceUnit = Literal["per_1k", "per_1m", "per_image"]

_UNIT_DIVISOR: dict[str, float] = {
    "per_1k": 1_000.0,
    "per_1m": 1_000_000.0,
    "per_image": 1.0,
}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: Candidate catalog filenames per discovery directory (first found wins).
_CATALOG_FILENAMES: tuple[str, ...] = ("pricing.yaml", "pricing.yml", "pricing.json")

#: ``nova.usage`` named field -> priceable usage type (ADR-0132 -> ADR-0133).
#: ``cache_write_tokens`` and ``total_tokens`` have no v0 price key and are
#: deliberately unmapped (an unpriced usage type contributes 0.0).
_USAGE_FIELD_TO_PRICING_TYPE: dict[str, str] = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cached_tokens": "cached",
    "reasoning_tokens": "reasoning",
    "audio_input_tokens": "audio",
    "audio_output_tokens": "audio",
    "image_input_tokens": "image",
    "image_output_tokens": "image",
}


class PricingCatalogError(Exception):
    """A pricing catalog file cannot be read, parsed, or validated."""


# ---------------------------------------------------------------------------
# Catalog models (mirror schemas/pricing-catalog.schema.json — closed shapes)
# ---------------------------------------------------------------------------


class Price(BaseModel):
    """One unit price: ``amount`` per ``unit`` in the entry's currency."""

    model_config = ConfigDict(extra="forbid")

    amount: float = Field(ge=0)
    unit: PriceUnit = "per_1k"


class UsagePricing(BaseModel):
    """Per-usage-type unit prices (all optional; ADR-0132 usage types)."""

    model_config = ConfigDict(extra="forbid")

    input: Price | None = None
    output: Price | None = None
    cached: Price | None = None
    reasoning: Price | None = None
    audio: Price | None = None
    image: Price | None = None


class Tier(BaseModel):
    """v0-minimal conditional pricing. Shape only — v0 resolution ignores it."""

    model_config = ConfigDict(extra="forbid")

    when: dict[str, Any]
    pricing: UsagePricing


class PricingEntry(BaseModel):
    """One catalog entry: prices for one ``model_id`` (optionally dated)."""

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1)
    pricing: UsagePricing
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    effective_from: date | None = None
    source: str | None = None
    tiers: list[Tier] | None = None

    @field_validator("effective_from", mode="before")
    @classmethod
    def _effective_from_shape(cls, value: object) -> object:
        """Accept a full-date string (``YYYY-MM-DD``) or a YAML-parsed date."""
        if value is None:
            return value
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, str) and _DATE_RE.fullmatch(value):
            return value  # calendar validity is checked by the date parser
        raise ValueError("effective_from must be a YYYY-MM-DD full date")


class PricingCatalog(BaseModel):
    """A whole catalog file: ``schema_version`` + ``models``."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"]
    models: list[PricingEntry]


# ---------------------------------------------------------------------------
# File loading and layer discovery
# ---------------------------------------------------------------------------


def load_catalog_file(path: Path) -> PricingCatalog:
    """Parse and validate one catalog file (YAML or JSON, by suffix).

    Raises :class:`PricingCatalogError` on any read, parse, or validation
    failure. Callers on capture paths must treat that as "skip this layer",
    never as a workload failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PricingCatalogError(f"cannot read pricing catalog {path}: {exc}") from exc
    suffix = path.suffix.lower()
    try:
        if suffix == ".json":
            data: object = json.loads(text)
        elif suffix in (".yaml", ".yml"):
            data = yaml.safe_load(text)
        else:
            raise PricingCatalogError(
                f"unsupported pricing catalog suffix {suffix!r} for {path} "
                "(expected .yaml, .yml, or .json)"
            )
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise PricingCatalogError(f"malformed pricing catalog {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise PricingCatalogError(
            f"malformed pricing catalog {path}: top level must be an object"
        )
    try:
        return PricingCatalog.model_validate(dict(data))
    except ValidationError as exc:
        raise PricingCatalogError(f"invalid pricing catalog {path}: {exc}") from exc


def user_catalog_dir() -> Path:
    """User-config discovery directory (honors ``$XDG_CONFIG_HOME``)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "novafabric"


def project_catalog_dir(cwd: Path | None = None) -> Path:
    """Project discovery directory: ``./.novafabric``."""
    return (cwd or Path.cwd()) / ".novafabric"


def find_catalog_file(directory: Path) -> Path | None:
    """First existing ``pricing.{yaml,yml,json}`` in ``directory``, else None."""
    for name in _CATALOG_FILENAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def builtin_entries() -> list[PricingEntry]:
    """The built-in ``PRICE_TABLE`` (ADR-0066) as catalog entries.

    Input/output per-1k USD prices only — exactly the shape the legacy
    ``_estimate_cost`` used, so pricing a known model through the merged
    catalog reproduces today's figures bit-for-bit.
    """
    from novafabric.cost.interceptor import (  # noqa: PLC0415 — avoid import cycle
        CostInterceptor,
    )

    return [
        PricingEntry(
            model_id=model_id,
            pricing=UsagePricing(
                input=Price(amount=input_per_1k, unit="per_1k"),
                output=Price(amount=output_per_1k, unit="per_1k"),
            ),
            currency="USD",
            source="built-in PRICE_TABLE (ADR-0066)",
        )
        for model_id, (input_per_1k, output_per_1k) in sorted(
            CostInterceptor.PRICE_TABLE.items()
        )
    ]


# ---------------------------------------------------------------------------
# Merge (D1) and content-addressing (D4)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MergedCatalog:
    """The merged, content-addressed view of all discovery layers."""

    #: model_id -> entries from the single winning layer (document order,
    #: deduplicated on ``effective_from`` with last-in-document-order wins).
    entries: Mapping[str, tuple[PricingEntry, ...]]
    #: model_id -> name of the layer that supplied its entries.
    layers: Mapping[str, str]
    #: ``sha256:<hex>`` over the canonicalized merged catalog (D4).
    digest: str
    #: Human-readable notes for layers that were skipped as malformed.
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class ResolvedPrice:
    """One resolved effective price: the entry plus its source layer."""

    entry: PricingEntry
    layer: str


def _entry_sort_key(entry: PricingEntry) -> str:
    return entry.effective_from.isoformat() if entry.effective_from else ""


def _catalog_digest(entries: Mapping[str, tuple[PricingEntry, ...]]) -> str:
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "models": {
            model_id: [
                entry.model_dump(mode="json", exclude_none=True)
                for entry in sorted(model_entries, key=_entry_sort_key)
            ]
            for model_id, model_entries in sorted(entries.items())
        },
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_merged_catalog(
    explicit: Path | None = None,
    cwd: Path | None = None,
) -> MergedCatalog:
    """Build the merged catalog from all discovery layers (D1).

    Pure function of the on-disk files and the built-in table — no network,
    no hidden state. A malformed layer (including ``explicit``) is skipped
    with a warning and recorded in ``MergedCatalog.warnings``; it never
    raises, so a capture is never blocked by a bad catalog. CLI commands that
    take ``--pricing-catalog`` validate the explicit file separately when a
    hard error is wanted.
    """
    layered: list[tuple[str, list[PricingEntry]]] = [("builtin", builtin_entries())]
    warnings: list[str] = []

    def _add_layer(layer: str, path: Path | None) -> None:
        if path is None:
            return
        try:
            catalog = load_catalog_file(path)
        except PricingCatalogError as exc:
            message = f"pricing catalog layer {layer!r} skipped: {exc}"
            log.warning("%s", message)
            warnings.append(message)
            return
        layered.append((layer, catalog.models))

    _add_layer("user", find_catalog_file(user_catalog_dir()))
    _add_layer("project", find_catalog_file(project_catalog_dir(cwd)))
    _add_layer("explicit", explicit)

    merged: dict[str, tuple[PricingEntry, ...]] = {}
    layer_of: dict[str, str] = {}
    for layer, entries in layered:
        # Group by model_id; within one file, the last entry in document
        # order wins for a duplicated (model_id, effective_from).
        by_model: dict[str, dict[date | None, PricingEntry]] = {}
        for entry in entries:
            by_model.setdefault(entry.model_id, {})[entry.effective_from] = entry
        for model_id, dated in by_model.items():
            # Per-entry replacement: this layer's entries fully replace any
            # lower layer's entries (including its whole price history).
            merged[model_id] = tuple(dated.values())
            layer_of[model_id] = layer

    return MergedCatalog(
        entries=merged,
        layers=layer_of,
        digest=_catalog_digest(merged),
        warnings=tuple(warnings),
    )


# ---------------------------------------------------------------------------
# Resolution (D3) and pricing math
# ---------------------------------------------------------------------------


def resolve_entry(
    catalog: MergedCatalog,
    model_id: str,
    at: date | None = None,
) -> ResolvedPrice | None:
    """Resolve the effective price for ``model_id`` as of ``at`` (D3).

    Selection: the dated entry with the latest ``effective_from <= at`` wins;
    undated entries lose to any eligible dated entry; a future-dated-only
    history with no undated entry resolves to None. Unknown ``model_id``
    resolves to None (the caller costs it 0.0, exactly as today).
    """
    entries = catalog.entries.get(model_id)
    if not entries:
        return None
    when = at or date.today()
    dated = [
        entry
        for entry in entries
        if entry.effective_from is not None and entry.effective_from <= when
    ]
    if dated:
        chosen = max(dated, key=lambda entry: entry.effective_from or when)
    else:
        undated = [entry for entry in entries if entry.effective_from is None]
        if not undated:
            return None
        chosen = undated[-1]
    return ResolvedPrice(entry=chosen, layer=catalog.layers[model_id])


def price_usage(entry: PricingEntry, usage: Mapping[str, int | float]) -> float:
    """Cost of ``usage`` (counts keyed by priceable usage type) under ``entry``.

    Honors each price's unit (``per_1k`` / ``per_1m`` / ``per_image``); a
    usage type the entry does not price contributes 0.0 (spec step 5). Each
    reported type is priced independently — no cross-type subtraction —
    matching the legacy ``_estimate_cost`` convention.
    """
    total = 0.0
    for usage_type in PRICING_USAGE_TYPES:
        count = usage.get(usage_type)
        if isinstance(count, bool) or not isinstance(count, (int, float)) or count <= 0:
            continue
        price: Price | None = getattr(entry.pricing, usage_type)
        if price is None:
            continue
        total += (count / _UNIT_DIVISOR[price.unit]) * price.amount
    return total


def _as_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def usage_counts_from_block(block: Mapping[str, Any]) -> dict[str, int]:
    """Map a ``nova.usage`` block (ADR-0132) onto priceable usage types.

    Audio and image input/output counts are summed under their single v0
    price key; ``cache_write_tokens``, ``total_tokens``, and ``extra`` keys
    are not priceable in v0 and are skipped.
    """
    counts: dict[str, int] = {}
    for field_name, usage_type in _USAGE_FIELD_TO_PRICING_TYPE.items():
        count = _as_count(block.get(field_name))
        if count is not None:
            counts[usage_type] = counts.get(usage_type, 0) + count
    return counts


def cost_for_model_call_record(
    record: Mapping[str, Any],
    catalog: MergedCatalog,
    at: date | None = None,
) -> dict[str, Any] | None:
    """Price one model-call record: recorded cost wins; else derive.

    - A recorded ``nova.cost`` block is returned verbatim with
      ``basis="recorded"`` — it is **never** overwritten or recomputed, even
      when the catalog also prices the model.
    - Otherwise the recorded token usage (``nova.usage`` when present, else
      the legacy ``gen_ai.usage.*`` scalars) is priced from the merged
      catalog and labeled ``basis="estimated"`` (ADR honesty rules), carrying
      the source layer and catalog digest for auditability.
    - No usage evidence or no catalog entry -> ``None`` (unpriced), exactly
      today's behavior for unmatched models.

    Pure function: the record is never mutated.
    """
    recorded = record.get("nova.cost")
    if isinstance(recorded, Mapping):
        amount = recorded.get("amount")
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            currency = recorded.get("currency")
            return {
                "currency": currency if isinstance(currency, str) and currency else "USD",
                "amount": float(amount),
                "basis": "recorded",
            }

    model = record.get("gen_ai.response.model") or record.get("gen_ai.request.model")
    if not isinstance(model, str) or not model:
        return None

    block = record.get("nova.usage")
    counts = usage_counts_from_block(block) if isinstance(block, Mapping) else {}
    if not counts:
        for key, usage_type in (
            ("gen_ai.usage.input_tokens", "input"),
            ("gen_ai.usage.output_tokens", "output"),
        ):
            count = _as_count(record.get(key))
            if count is not None:
                counts[usage_type] = count
    if not counts:
        return None

    resolved = resolve_entry(catalog, model, at=at)
    if resolved is None:
        return None
    return {
        "currency": resolved.entry.currency,
        "amount": price_usage(resolved.entry, counts),
        "basis": "estimated",
        "model_id": model,
        "pricing_source_layer": resolved.layer,
        "pricing_catalog_digest": catalog.digest,
    }
