"""Pluggable PII masking pipeline (ADR-0135) — **experimental**.

Operator-registered maskers that run at capture time *after* the built-in
ADR-0009 secret scanners and *before* the capsule is finalized. Built-ins
always run and are never disabled by plugins; every mask and every masker
failure is recorded in the capsule's ``redaction-proof.json``
(``masker_findings[]`` / ``masker_errors[]``), fail-closed: a crashing,
hanging, or invalid masker redacts the field — it never un-redacts, never
leaks a raw value, and never blocks the captured workload.

Spec: ``design/spec/pii-masking-pipeline-v0.md``. Schemas:
``schemas/masking-config.schema.json``, ``schemas/masker-finding.schema.json``,
``schemas/masker-error.schema.json``.
"""
from novafabric.masking._models import (
    DEFAULT_MAX_INPUT_BYTES,
    DEFAULT_TIMEOUT_MS,
    UNCHANGED,
    MaskContext,
    MaskerProtocol,
    MaskerSpec,
    MaskField,
    MaskingBlock,
    MaskingConfig,
    MaskingConfigError,
    load_masking_config,
)
from novafabric.masking._pipeline import MaskingPipeline
from novafabric.masking._registry import (
    ENTRY_POINT_GROUP,
    LoadedMasker,
    MaskerRegistrationError,
    load_maskers,
    resolve_masker,
)

__all__ = [
    "DEFAULT_MAX_INPUT_BYTES",
    "DEFAULT_TIMEOUT_MS",
    "ENTRY_POINT_GROUP",
    "UNCHANGED",
    "LoadedMasker",
    "MaskContext",
    "MaskField",
    "MaskerProtocol",
    "MaskerRegistrationError",
    "MaskerSpec",
    "MaskingBlock",
    "MaskingConfig",
    "MaskingConfigError",
    "MaskingPipeline",
    "load_maskers",
    "load_masking_config",
    "resolve_masker",
]
