"""Data contracts for the pluggable PII masking pipeline (ADR-0135).

Status: **experimental** — the masker plugin surface may change between
minor versions until the companion spec (pii-masking-pipeline-v0) freezes.

A *masker* is a pure, deterministic callable an operator registers so that
imperative, org-specific masking logic (checksum-validated IDs, domain
identifiers, format-preserving tokenizers) runs at capture time **after**
the built-in ADR-0009 secret scanner and **before** the capsule is
finalized. Built-ins are never disabled by maskers; a masker failure never
un-redacts and never blocks capture (fail-closed on secrets, fail-safe for
the workload).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

DEFAULT_TIMEOUT_MS = 50
DEFAULT_MAX_INPUT_BYTES = 65536


class _Unchanged:
    """Sentinel a masker returns to decline masking a value."""

    _instance: "_Unchanged | None" = None

    def __new__(cls) -> "_Unchanged":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNCHANGED"


UNCHANGED = _Unchanged()
"""Return this from ``mask()`` to decline (leave the value untouched)."""


@dataclass(frozen=True)
class MaskField:
    """Locator of the string value being offered to a masker.

    ``target_ref`` mirrors the redaction proof's ``target_ref`` locator,
    e.g. ``model-calls.jsonl#L4 messages[2].content``.
    """

    target_kind: str
    target_ref: str
    byte_offset: int = 0


@dataclass(frozen=True)
class MaskContext:
    """Read-only run metadata handed to a masker.

    Deliberately contains **no network handle and no capsule write
    handle** — a masker returns a value; the pipeline writes and proves.
    """

    run_id: str
    tenant: str = "local"
    masker_config: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class MaskerProtocol(Protocol):
    """The contract every registered masker must satisfy (ADR-0135 D1).

    A masker MUST be pure, deterministic, and offline: identical
    ``(field, value, context)`` yields identical output; no sockets, no
    filesystem writes, no capsule mutation.
    """

    masker_id: str
    masker_version: str
    pattern_ids: Sequence[str]

    def mask(
        self, field: MaskField, value: str, context: MaskContext
    ) -> "str | _Unchanged":
        """Return the masked string, or ``UNCHANGED`` to decline."""
        ...


class MaskingConfigError(Exception):
    """A masking.yaml file is missing, unreadable, or invalid."""


class MaskerSpec(BaseModel):
    """One ``masking.maskers[]`` entry (tracks masking-config.schema.json)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    version: str | None = None
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, gt=0)
    max_input_bytes: int = Field(default=DEFAULT_MAX_INPUT_BYTES, gt=0)
    on_error: Literal["redact", "drop"] = "redact"
    config: dict[str, Any] = Field(default_factory=dict)


class MaskingBlock(BaseModel):
    """The ``masking`` object of masking.yaml."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    maskers: list[MaskerSpec]


class MaskingConfig(BaseModel):
    """Root of ``.novafabric/masking.yaml``. Absent file ⇒ ADR-0009 behavior."""

    model_config = ConfigDict(extra="forbid")

    masking: MaskingBlock


def load_masking_config(path: Path) -> MaskingConfig:
    """Load and validate a ``masking.yaml`` file (fail-closed on errors)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MaskingConfigError(f"cannot read masking config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise MaskingConfigError(f"invalid YAML in masking config {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise MaskingConfigError(
            f"masking config {path} must be a mapping with a 'masking' block"
        )
    try:
        return MaskingConfig.model_validate(data)
    except ValidationError as exc:
        raise MaskingConfigError(f"invalid masking config {path}: {exc}") from exc
