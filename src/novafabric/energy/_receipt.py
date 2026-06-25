"""Energy-Anchored Action Receipt model + honest-degradation builders (ADR-0093).

A receipt records the energy attributed to one consequential agent action under
the load-bearing invariant *measured-or-declared-unknown, never fabricated*:

- ``confidence="measured"`` — a direct counter read at action scope;
- ``confidence="apportioned"`` — a measured coarser total shared down to the action;
- ``confidence="declared"`` — a published-table estimate, never measured here;
- ``confidence="unknown"`` — no figure at all (``measured_joules is None``).

The model intentionally does **not** hard-reject inconsistent receipts at
construction, so that a verifier (``nova energy verify``, slice A3) can load a
tampered/forged receipt from disk and report on it. Callers that build receipts
in-process should use :func:`declared_receipt` / :func:`unavailable_receipt`
(and, later, the measured builders), which only ever produce honest records.
Use :meth:`EnergyReceipt.consistency_errors` to check a receipt's honesty.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from novafabric.capsule.ulid_util import new_ulid

SCHEMA_VERSION = "0.1.0"


class _StrEnum(str, Enum):
    """str-valued enum that serialises to its value."""


class ActionKind(_StrEnum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    JOB_STEP = "job_step"
    RUN = "run"


class MeasurementSource(_StrEnum):
    NVML = "nvml"
    RAPL_PKG = "rapl_pkg"
    RAPL_DRAM = "rapl_dram"
    IPMI_BMC = "ipmi_bmc"
    SACCT_ENERGY = "sacct_energy"
    CGROUP_V2 = "cgroup_v2"
    DECLARED_MODEL_TABLE = "declared_model_table"
    DECLARED_TDP = "declared_tdp"
    UNAVAILABLE = "unavailable"


class MeasurementScope(_StrEnum):
    PER_PROCESS = "per_process"
    PER_CGROUP = "per_cgroup"
    PER_JOB = "per_job"
    PER_PACKAGE = "per_package"
    PER_NODE = "per_node"
    NONE = "none"


class Confidence(_StrEnum):
    MEASURED = "measured"
    APPORTIONED = "apportioned"
    DECLARED = "declared"
    UNKNOWN = "unknown"


class AttributionMethod(_StrEnum):
    DIRECT_COUNTER = "direct_counter"
    TIME_SHARE = "time_share"
    ENERGY_SHARE = "energy_share"
    TOKEN_SHARE = "token_share"
    GPU_UTIL_SHARE = "gpu_util_share"
    NONE = "none"


class CarbonAccountingMode(_StrEnum):
    LOCATION_BASED = "location_based"
    MARKET_BASED = "market_based"
    DECLARED = "declared"
    UNKNOWN = "unknown"


class AcceleratorKind(_StrEnum):
    NONE = "none"
    NVIDIA_GPU = "nvidia_gpu"
    AMD_GPU = "amd_gpu"
    INTEL_GPU = "intel_gpu"
    TPU = "tpu"
    OTHER = "other"


class CounterName(_StrEnum):
    NVML = "nvml"
    RAPL_PKG = "rapl_pkg"
    RAPL_DRAM = "rapl_dram"
    IPMI_BMC = "ipmi_bmc"
    SACCT_ENERGY = "sacct_energy"
    CGROUP_V2 = "cgroup_v2"


#: measurement sources that are estimates, not physical readings
_DECLARED_SOURCES = {
    MeasurementSource.DECLARED_MODEL_TABLE,
    MeasurementSource.DECLARED_TDP,
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class ActionRef(_Model):
    """Join key binding a receipt to one action elsewhere in the capsule."""

    kind: ActionKind
    id: str = Field(min_length=1)


class Sampling(_Model):
    interval_ms: int = Field(ge=0)
    sample_count: int = Field(ge=0)
    integrator: str


class ErrorModel(_Model):
    kind: str
    value: float | list[float] | None = None
    basis: str | None = None


class GridIntensitySource(_Model):
    provider: str
    region: str
    method: str
    retrieved_at: str
    signal_url: str | None = None
    double_count_guard: str | None = None


class Hardware(_Model):
    node_id: str
    cpu_model: str | None = None
    accelerator_kind: AcceleratorKind | None = None
    gpu_uuid: str | None = None
    tdp_watts: float | None = None
    counters_available: list[CounterName] = Field(default_factory=list)


class Seal(_Model):
    signed: bool = False
    dsse_envelope_ref: str | None = None
    merkle_leaf_hash: str | None = None
    timestamp_ref: str | None = None


class EnergyReceipt(_Model):
    """An Energy-Anchored Action Receipt (ADR-0093, schema 0.1.0)."""

    schema_version: str = SCHEMA_VERSION
    receipt_id: str = Field(default_factory=new_ulid)
    action_ref: ActionRef
    run_id: str
    capsule_id: str = Field(min_length=1)
    measured_joules: float | None
    measurement_source: MeasurementSource
    measurement_scope: MeasurementScope
    wall_clock_s: float | None = None
    sampling: Sampling | None = None
    confidence: Confidence
    error_model: ErrorModel | None = None
    attribution_method: AttributionMethod
    attribution_basis: str | None = None
    idle_baseline_j: float | None = None
    carbon_g_co2e: float | None
    grid_intensity_g_per_kwh: float | None
    grid_intensity_source: GridIntensitySource | None = None
    carbon_accounting_mode: CarbonAccountingMode
    hardware: Hardware
    payload_hash: str = ""
    seal: Seal | None = None
    generated_at: str

    # --- payload hashing ----------------------------------------------------

    def canonical_payload_bytes(self) -> bytes:
        """JCS-style canonical bytes of the receipt, excluding the mutable
        ``seal`` block and the ``payload_hash`` field itself.

        The agent hot path emits only this hash; the finalizer/collector signs
        it (ADR-0093 §A3 — no heavy crypto in the hot path).
        """
        body = self.model_dump(
            mode="json", exclude_none=True, exclude={"seal", "payload_hash"}
        )
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()

    def compute_payload_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.canonical_payload_bytes()).hexdigest()

    #: schema-required fields that are nullable and must survive serialisation
    _REQUIRED_NULLABLE = ("measured_joules", "carbon_g_co2e", "grid_intensity_g_per_kwh")

    def to_record(self) -> dict[str, object]:
        """Serialise to a schema-valid JSON object for ``energy-receipts.jsonl``.

        Drops unset optional fields (so optional non-nullable objects like
        ``sampling``/``seal`` are absent rather than ``null``), but keeps the
        required-nullable fields (``measured_joules`` etc.) present as ``null``.
        """
        body: dict[str, object] = self.model_dump(mode="json", exclude_none=True)
        for key in self._REQUIRED_NULLABLE:
            body.setdefault(key, getattr(self, key))
        return body

    def with_payload_hash(self) -> EnergyReceipt:
        return self.model_copy(update={"payload_hash": self.compute_payload_hash()})

    # --- honesty / forgery guard -------------------------------------------

    def consistency_errors(self) -> list[str]:
        """Return the receipt's honesty violations (empty == consistent).

        This is the structural forgery guard used by ``nova energy verify``:
        a receipt may not claim a measurement it cannot back.
        """
        errors: list[str] = []
        src = self.measurement_source
        conf = self.confidence
        counters = self.hardware.counters_available

        if conf is Confidence.MEASURED and not counters:
            errors.append(
                "confidence='measured' but no counter is available on the node"
            )
        if src in _DECLARED_SOURCES and conf is not Confidence.DECLARED:
            errors.append(
                f"declared source {src.value} must set confidence='declared'"
            )
        if src is MeasurementSource.UNAVAILABLE:
            if self.measured_joules is not None:
                errors.append("source='unavailable' must have measured_joules=null")
            if conf is not Confidence.UNKNOWN:
                errors.append("source='unavailable' must set confidence='unknown'")
        if conf is Confidence.UNKNOWN and self.measured_joules is not None:
            errors.append("confidence='unknown' must have measured_joules=null")
        if conf is not Confidence.UNKNOWN and self.measured_joules is None:
            errors.append(f"confidence='{conf.value}' must have a measured_joules value")
        if self.carbon_g_co2e is not None and self.measured_joules is None:
            errors.append("carbon_g_co2e requires a non-null measured_joules")
        return errors


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unavailable_receipt(
    *,
    action_ref: ActionRef,
    run_id: str,
    capsule_id: str,
    node_id: str,
    counters_available: Iterable[CounterName | str] = (),
    wall_clock_s: float | None = None,
    generated_at: str | None = None,
) -> EnergyReceipt:
    """Build the honest no-measurement receipt.

    The common case on hardware that cannot attribute per-process energy
    (true on n1, true on the GB10 SoC per arXiv 2605.27599). ``counters_available``
    records what the node *could* read, so an auditor can tell "hardware cannot"
    from "NovaFabric failed to read it".
    """
    counters = [CounterName(c) for c in counters_available]
    receipt = EnergyReceipt(
        action_ref=action_ref,
        run_id=run_id,
        capsule_id=capsule_id,
        measured_joules=None,
        measurement_source=MeasurementSource.UNAVAILABLE,
        measurement_scope=MeasurementScope.NONE,
        wall_clock_s=wall_clock_s,
        confidence=Confidence.UNKNOWN,
        attribution_method=AttributionMethod.NONE,
        carbon_g_co2e=None,
        grid_intensity_g_per_kwh=None,
        carbon_accounting_mode=CarbonAccountingMode.UNKNOWN,
        hardware=Hardware(node_id=node_id, counters_available=counters),
        generated_at=generated_at or _now_iso(),
    )
    return receipt.with_payload_hash()


def declared_receipt(
    *,
    action_ref: ActionRef,
    run_id: str,
    capsule_id: str,
    node_id: str,
    declared_joules: float,
    source: MeasurementSource = MeasurementSource.DECLARED_MODEL_TABLE,
    basis: str = "declared estimate",
    wall_clock_s: float | None = None,
    generated_at: str | None = None,
) -> EnergyReceipt:
    """Build an honestly-labelled *declared* (estimated, not measured) receipt.

    This is where the existing forgeable ``energy_joules_estimated`` price-table
    figure is reclassified: it keeps its number but is stamped
    ``confidence="declared"`` so it can never masquerade as a measurement.
    """
    if source not in _DECLARED_SOURCES:
        raise ValueError(f"declared_receipt requires a declared source, got {source}")
    receipt = EnergyReceipt(
        action_ref=action_ref,
        run_id=run_id,
        capsule_id=capsule_id,
        measured_joules=declared_joules,
        measurement_source=source,
        measurement_scope=MeasurementScope.NONE,
        wall_clock_s=wall_clock_s,
        confidence=Confidence.DECLARED,
        attribution_method=AttributionMethod.NONE,
        error_model=ErrorModel(kind="none", basis=basis),
        carbon_g_co2e=None,
        grid_intensity_g_per_kwh=None,
        carbon_accounting_mode=CarbonAccountingMode.DECLARED,
        hardware=Hardware(node_id=node_id, counters_available=[]),
        generated_at=generated_at or _now_iso(),
    )
    return receipt.with_payload_hash()


__all__: Sequence[str] = (
    "SCHEMA_VERSION",
    "ActionKind",
    "ActionRef",
    "AcceleratorKind",
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
)
