"""Energy-Anchored Action Receipts (ADR-0093, Accountability Spine feature A).

Measured-or-declared-unknown energy + carbon, bound (in later slices) into the
NovaSeal/Evidence machinery. See ``design/architecture/energy-anchored-receipts.md``.
"""

from __future__ import annotations

from novafabric.energy._receipt import (
    SCHEMA_VERSION,
    AcceleratorKind,
    ActionKind,
    ActionRef,
    AttributionMethod,
    CarbonAccountingMode,
    Confidence,
    CounterName,
    EnergyReceipt,
    ErrorModel,
    GridIntensitySource,
    Hardware,
    MeasurementScope,
    MeasurementSource,
    Sampling,
    Seal,
    declared_receipt,
    unavailable_receipt,
)

__all__ = [
    "SCHEMA_VERSION",
    "AcceleratorKind",
    "ActionKind",
    "ActionRef",
    "AttributionMethod",
    "CarbonAccountingMode",
    "Confidence",
    "CounterName",
    "EnergyReceipt",
    "ErrorModel",
    "GridIntensitySource",
    "Hardware",
    "MeasurementScope",
    "MeasurementSource",
    "Sampling",
    "Seal",
    "declared_receipt",
    "unavailable_receipt",
]
