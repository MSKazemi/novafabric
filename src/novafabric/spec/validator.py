from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from rich.console import Console

import yaml
from pydantic import TypeAdapter, ValidationError

from novafabric.spec.models import AssetSpec, AssetStatus, AssetType, BaseAssetSpec


class SpecValidationError(Exception):
    def __init__(
        self,
        message: str,
        errors: list[Any] | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.errors = errors or []
        self.hint = hint


def print_spec_error(
    console: "Console", spec_file: "Path", exc: SpecValidationError
) -> None:
    console.print(f"[red]✗[/red] {spec_file}")
    console.print(f"  [bold red]{exc}[/bold red]")
    if exc.hint:
        console.print(f"\n  [yellow]Hint:[/yellow] {exc.hint}")


_adapter: TypeAdapter[Any] = TypeAdapter(AssetSpec)


def _hint_for_error(error: Any) -> str | None:
    loc = ".".join(str(p) for p in error.get("loc", []))
    error_type = error.get("type", "")
    msg = error.get("msg", "")
    ctx = error.get("ctx", {}) or {}

    if error_type == "missing":
        return f"Add required field '{loc}' to your YAML spec."
    # Pydantic v2 emits 'union_tag_invalid' when a discriminated-union
    # tag (asset_type) is unknown. The discriminator name is in
    # ctx.discriminator (single-quoted, e.g. "'asset_type'").
    if error_type == "union_tag_invalid":
        discriminator = str(ctx.get("discriminator", "")).strip("'\"")
        if discriminator == "asset_type":
            valid = ", ".join(t.value for t in AssetType)
            return f"Valid asset_type values: {valid}"
        if discriminator:
            return f"Unknown {discriminator}. Check the YAML spec."
    if error_type in ("enum", "literal_error"):
        expected = ctx.get("expected", "")
        if "asset_type" in loc:
            valid = ", ".join(t.value for t in AssetType)
            return f"Valid asset_type values: {valid}"
        if "status" in loc:
            valid = ", ".join(s.value for s in AssetStatus)
            return f"Valid status values: {valid}"
        if expected:
            return f"Expected one of: {expected}"
    if "semver" in msg.lower() or ("version" in loc and error_type == "value_error"):
        return "Use semver format: 1.0.0, v1.0.0, or v1.2.3-rc.1"
    if error_type == "too_short" and "evals" in loc:
        return "Agents require at least one eval suite: add a name under spec.evals."
    return None


def validate_spec(yaml_path: Path) -> BaseAssetSpec:
    if not yaml_path.exists():
        raise SpecValidationError(
            f"File not found: {yaml_path}",
            hint="Check the path and try again.",
        )

    try:
        with open(yaml_path) as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise SpecValidationError(
            f"YAML parse error in {yaml_path.name}: {exc}",
            hint="Check for unbalanced quotes, bad indentation, or invalid characters.",
        ) from exc

    if not isinstance(data, dict):
        raise SpecValidationError(
            "YAML file must contain a mapping at the top level",
            hint="The file should start with key: value pairs, not a list or scalar.",
        )

    try:
        return cast(BaseAssetSpec, _adapter.validate_python(data))
    except ValidationError as exc:
        errors = exc.errors()
        first = errors[0]
        loc = ".".join(str(p) for p in first["loc"])
        msg = first["msg"]
        hint = _hint_for_error(first)
        raise SpecValidationError(
            f"Validation error on field '{loc}': {msg}",
            errors=errors,
            hint=hint,
        ) from exc


# Alias used by spec/__init__.py and registry/service.py
validate_asset_spec = validate_spec
