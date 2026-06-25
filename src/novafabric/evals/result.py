from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class Metric(BaseModel):
    model_config = {"extra": "forbid"}

    name: str
    value: float
    unit: str
    threshold: float
    passed: bool


class StatisticalContext(BaseModel):
    model_config = {"extra": "forbid"}

    sample_size: Optional[int] = None
    confidence_interval: Optional[list[float]] = None  # exactly 2 elements
    p_value: Optional[float] = None  # must be in [0, 1]

    @field_validator("p_value")
    @classmethod
    def p_value_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("p_value must be in [0, 1]")
        return v

    @field_validator("confidence_interval")
    @classmethod
    def ci_two_elements(cls, v: Optional[list[float]]) -> Optional[list[float]]:
        if v is not None and len(v) != 2:
            raise ValueError("confidence_interval must have exactly 2 elements")
        return v


class EvalResult(BaseModel):
    model_config = {"extra": "forbid"}

    schema_version: str = Field(default="0.1.0")
    suite_id: str
    suite_version: str
    oci_digest: str  # "sha256:..." or "" for host-env adapters
    run_at: datetime
    capsule_id: str
    passed: bool
    metrics: list[Metric]  # minItems 1
    statistical_context: Optional[StatisticalContext] = None
    notes: Optional[str] = None

    @field_validator("metrics")
    @classmethod
    def at_least_one_metric(cls, v: list[Metric]) -> list[Metric]:
        if len(v) < 1:
            raise ValueError("metrics must contain at least one entry")
        return v
