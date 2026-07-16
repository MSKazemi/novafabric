"""`nova pricing` — local model-pricing catalog commands (ADR-0133).

Experimental. A pricing catalog is a single local YAML/JSON file merged over
the built-in ``PRICE_TABLE`` (layers: builtin < user < project < explicit).
All read-only surfacing (``list``/``show``) plus an idempotent write path
(``add``). Fully offline — no remote registry, no price fetch. Prices remain
user-asserted estimates; ``cost_usd_estimated`` stays an estimate (ADR-0066).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, NoReturn, Optional

import typer
import yaml

from novafabric.cost.pricing_catalog import (
    SCHEMA_VERSION,
    MergedCatalog,
    Price,
    PricingCatalogError,
    PricingEntry,
    UsagePricing,
    load_catalog_file,
    load_merged_catalog,
    project_catalog_dir,
    resolve_entry,
    user_catalog_dir,
)

app = typer.Typer(
    help="Local model-pricing catalog: list, show, add (ADR-0133, experimental).",
    no_args_is_help=True,
)

_CATALOG_OPTION = typer.Option(
    None,
    "--pricing-catalog",
    help="Explicit catalog file (highest-precedence layer).",
)


def _fail(message: str, *, code: int = 1) -> NoReturn:
    typer.echo(f"Pricing error: {message}", err=True)
    raise SystemExit(code)


def _parse_at(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(f"--at must be a YYYY-MM-DD date, got {value!r}")


def _merged(explicit: Path | None) -> MergedCatalog:
    """Merged catalog for CLI use: an explicit file must be valid."""
    if explicit is not None:
        try:
            load_catalog_file(explicit)
        except PricingCatalogError as exc:
            _fail(str(exc))
    catalog = load_merged_catalog(explicit=explicit)
    for warning in catalog.warnings:
        typer.echo(f"Warning: {warning}", err=True)
    return catalog


def _price_cell(price: Price | None) -> str:
    if price is None:
        return "-"
    return f"{price.amount:g}/{price.unit.removeprefix('per_')}"


def _entry_row(entry: PricingEntry, layer: str) -> dict[str, Any]:
    return {
        "model_id": entry.model_id,
        "layer": layer,
        "currency": entry.currency,
        "effective_from": entry.effective_from.isoformat() if entry.effective_from else "",
        "input": _price_cell(entry.pricing.input),
        "output": _price_cell(entry.pricing.output),
        "cached": _price_cell(entry.pricing.cached),
        "reasoning": _price_cell(entry.pricing.reasoning),
        "source": entry.source or "",
    }


def _format_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "(no entries)"
    widths = {h: len(h) for h in headers}
    for row in rows:
        for h in headers:
            widths[h] = max(widths[h], len(str(row.get(h, ""))))
    sep = "  "
    lines = [
        sep.join(h.ljust(widths[h]) for h in headers),
        sep.join("-" * widths[h] for h in headers),
    ]
    lines.extend(
        sep.join(str(row.get(h, "")).ljust(widths[h]) for h in headers) for row in rows
    )
    return "\n".join(lines)


@app.command("list")
def pricing_list_cmd(
    pricing_catalog: Optional[Path] = _CATALOG_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List the merged pricing catalog, showing each entry's source layer.

    Layers merge builtin < user (~/.config/novafabric/pricing.yaml) <
    project (./.novafabric/pricing.yaml) < --pricing-catalog PATH; a higher
    layer's entries fully replace a lower layer's for the same model_id.
    """
    catalog = _merged(pricing_catalog)
    rows = [
        _entry_row(entry, catalog.layers[model_id])
        for model_id in sorted(catalog.entries)
        for entry in catalog.entries[model_id]
    ]
    if as_json:
        payload = {
            "pricing_catalog_digest": catalog.digest,
            "entries": [
                {
                    "layer": catalog.layers[model_id],
                    **entry.model_dump(mode="json", exclude_none=True),
                }
                for model_id in sorted(catalog.entries)
                for entry in catalog.entries[model_id]
            ],
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    headers = [
        "model_id",
        "layer",
        "currency",
        "effective_from",
        "input",
        "output",
        "cached",
        "reasoning",
        "source",
    ]
    typer.echo(_format_table(rows, headers))
    typer.echo(f"\ncatalog digest: {catalog.digest}")


@app.command("show")
def pricing_show_cmd(
    model_id: str = typer.Argument(..., help="Model ID to resolve."),
    at: Optional[str] = typer.Option(
        None, "--at", help="Resolve the price in force at DATE (YYYY-MM-DD)."
    ),
    pricing_catalog: Optional[Path] = _CATALOG_OPTION,
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Resolve and print the effective price for one model.

    Exits 1 when the model is in no layer (its cost would be 0.0, exactly
    the pre-catalog behavior for unknown models).
    """
    at_date = _parse_at(at)
    catalog = _merged(pricing_catalog)
    resolved = resolve_entry(catalog, model_id, at=at_date)
    if resolved is None:
        _fail(
            f"model {model_id!r} is not priced by any catalog layer "
            "(its estimated cost is 0.0); add one with `nova pricing add`"
        )
    entry = resolved.entry
    if as_json:
        payload = {
            "layer": resolved.layer,
            "pricing_catalog_digest": catalog.digest,
            **entry.model_dump(mode="json", exclude_none=True),
        }
        typer.echo(json.dumps(payload, indent=2))
        return
    typer.echo(f"model_id:       {entry.model_id}")
    typer.echo(f"layer:          {resolved.layer}")
    typer.echo(f"currency:       {entry.currency}")
    if entry.effective_from:
        typer.echo(f"effective_from: {entry.effective_from.isoformat()}")
    if entry.source:
        typer.echo(f"source:         {entry.source}")
    for usage_type in ("input", "output", "cached", "reasoning", "audio", "image"):
        price: Price | None = getattr(entry.pricing, usage_type)
        if price is not None:
            typer.echo(
                f"{usage_type + ':':<15} {price.amount:g} {entry.currency} {price.unit}"
            )
    typer.echo(f"catalog digest: {catalog.digest}")


def _price_or_none(amount: float | None, unit: str) -> Price | None:
    if amount is None:
        return None
    return Price(amount=amount, unit=unit)  # type: ignore[arg-type]


@app.command("add")
def pricing_add_cmd(
    model_id: str = typer.Argument(..., help="Model ID the prices apply to."),
    input_price: Optional[float] = typer.Option(
        None, "--input", help="Input-token price (in --unit)."
    ),
    output_price: Optional[float] = typer.Option(
        None, "--output", help="Output-token price (in --unit)."
    ),
    cached_price: Optional[float] = typer.Option(
        None, "--cached", help="Cache-read token price (in --unit)."
    ),
    reasoning_price: Optional[float] = typer.Option(
        None, "--reasoning", help="Reasoning-token price (in --unit)."
    ),
    audio_price: Optional[float] = typer.Option(
        None, "--audio", help="Audio-token price (in --unit)."
    ),
    image_price: Optional[float] = typer.Option(
        None, "--image", help="Image-unit price (priced per_image)."
    ),
    unit: str = typer.Option(
        "per_1k", "--unit", help="Token price unit: per_1k | per_1m."
    ),
    currency: str = typer.Option("USD", "--currency", help="ISO-4217 currency code."),
    effective_from: Optional[str] = typer.Option(
        None, "--effective-from", help="Date the price takes effect (YYYY-MM-DD)."
    ),
    source: Optional[str] = typer.Option(
        None, "--source", help="Freeform provenance note."
    ),
    user: bool = typer.Option(
        False, "--user", help="Write to the user catalog instead of the project one."
    ),
    catalog_path: Optional[Path] = typer.Option(
        None, "--catalog", help="Write to this catalog file explicitly."
    ),
) -> None:
    """Append or update an entry in the nearest writable catalog.

    Defaults to the project catalog (./.novafabric/pricing.yaml); use --user
    for ~/.config/novafabric/pricing.yaml or --catalog PATH for an explicit
    file. Idempotent per (model_id, effective_from): re-adding replaces the
    matching entry instead of duplicating it.
    """
    if unit not in ("per_1k", "per_1m"):
        _fail(f"--unit must be per_1k or per_1m, got {unit!r}")
    if all(
        price is None
        for price in (
            input_price,
            output_price,
            cached_price,
            reasoning_price,
            audio_price,
            image_price,
        )
    ):
        _fail("provide at least one price (--input/--output/--cached/...)")
    effective = _parse_at(effective_from)
    try:
        entry = PricingEntry(
            model_id=model_id,
            pricing=UsagePricing(
                input=_price_or_none(input_price, unit),
                output=_price_or_none(output_price, unit),
                cached=_price_or_none(cached_price, unit),
                reasoning=_price_or_none(reasoning_price, unit),
                audio=_price_or_none(audio_price, unit),
                image=_price_or_none(image_price, "per_image"),
            ),
            currency=currency,
            effective_from=effective,
            source=source,
        )
    except ValueError as exc:
        _fail(f"invalid entry: {exc}")

    if catalog_path is not None:
        target = catalog_path
    elif user:
        target = user_catalog_dir() / "pricing.yaml"
    else:
        target = project_catalog_dir() / "pricing.yaml"

    if target.exists():
        try:
            catalog = load_catalog_file(target)
        except PricingCatalogError as exc:
            _fail(f"refusing to modify malformed catalog: {exc}")
        models = list(catalog.models)
    else:
        models = []

    replaced = False
    for index, existing in enumerate(models):
        if (
            existing.model_id == entry.model_id
            and existing.effective_from == entry.effective_from
        ):
            models[index] = entry
            replaced = True
            break
    if not replaced:
        models.append(entry)

    document = {
        "schema_version": SCHEMA_VERSION,
        "models": [m.model_dump(mode="json", exclude_none=True) for m in models],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.suffix.lower() == ".json":
        text = json.dumps(document, indent=2) + "\n"
    elif target.suffix.lower() in (".yaml", ".yml"):
        text = yaml.safe_dump(document, sort_keys=False, default_flow_style=False)
    else:
        _fail(f"unsupported catalog suffix {target.suffix!r} (use .yaml, .yml, or .json)")
    target.write_text(text, encoding="utf-8")
    action = "updated" if replaced else "added"
    dated = f" (effective_from {entry.effective_from})" if entry.effective_from else ""
    typer.echo(f"{action} pricing for {model_id!r}{dated} in {target}")
