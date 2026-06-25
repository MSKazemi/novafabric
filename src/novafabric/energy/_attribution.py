"""Attribute energy to a capsule's actions (ADR-0093 §A1/§A4).

Runs at capsule finalization (cold path), reading the action streams and the
energy samples (when present) to emit one :class:`EnergyReceipt` per action into
``energy-receipts.jsonl``. The degrade-safe default — no per-action counter —
produces honest ``unavailable`` receipts, the common case on hardware that
cannot attribute per-process energy.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import yaml

from novafabric.capsule.ulid_util import new_ulid
from novafabric.energy._readers import _RAPL_BASE, probe_counters, rapl_delta_joules
from novafabric.energy._receipt import (
    ActionKind,
    ActionRef,
    AttributionMethod,
    CarbonAccountingMode,
    Confidence,
    CounterName,
    EnergyReceipt,
    Hardware,
    MeasurementScope,
    MeasurementSource,
    _now_iso,
    unavailable_receipt,
)
from novafabric.energy._sampler import SAMPLES_FILE

#: capsule action streams and the action kind each records
_ACTION_STREAMS: dict[str, ActionKind] = {
    "model-calls.jsonl": ActionKind.MODEL_CALL,
    "tool-calls.jsonl": ActionKind.TOOL_CALL,
}

#: candidate keys probed (in order) for an action id
_ID_KEYS = ("model_call_id", "tool_call_id", "call_id", "event_id", "id", "span_id")

RECEIPTS_FILE = "energy-receipts.jsonl"


def _iter_jsonl(path: Path) -> Iterator[dict[str, object]]:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            yield record


def _first_id(record: dict[str, object]) -> str:
    for key in _ID_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return new_ulid()


def _read_run_id(capsule_dir: Path) -> str:
    path = capsule_dir / "capsule.yaml"
    try:
        loaded = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return new_ulid()
    if isinstance(loaded, dict):
        run_id = loaded.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return new_ulid()


def _iter_actions(
    capsule_dir: Path,
) -> Iterator[tuple[ActionKind, dict[str, object]]]:
    """Yield ``(kind, record)`` for every action across the capsule streams."""
    for stream, kind in _ACTION_STREAMS.items():
        for record in _iter_jsonl(capsule_dir / stream):
            yield kind, record


def _action_wall_clock_s(record: dict[str, object]) -> float | None:
    """Best-effort wall-clock seconds for an action.

    Prefers an explicit ``duration_ms``/``duration_s``; else differences two
    ISO-8601 ``started``/``finished`` timestamps. Returns ``None`` when no
    timing is recorded (caller falls back to an even split).
    """
    for key, scale in (("duration_ms", 1000.0), ("duration_s", 1.0)):
        value = record.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return float(value) / scale
    started, finished = record.get("started"), record.get("finished")
    if isinstance(started, str) and isinstance(finished, str):
        try:
            t0 = datetime.fromisoformat(started)
            t1 = datetime.fromisoformat(finished)
        except ValueError:
            return None
        delta = (t1 - t0).total_seconds()
        if delta >= 0:
            return delta
    return None


def _integrate_node_joules(capsule_dir: Path) -> tuple[float, CounterName] | None:
    """Integrate node joules from ``energy-samples.jsonl`` (wraparound-safe).

    Sums per-domain deltas between the first and last non-gap samples. Returns
    ``(joules, counter)`` or ``None`` when no usable (non-gap) reading pair
    exists — the honest "no node total" signal that triggers the unavailable
    fallback.
    """
    readings: list[dict[str, int]] = []
    for record in _iter_jsonl(capsule_dir / SAMPLES_FILE):
        if record.get("gap") is True:
            continue
        domains = record.get("domains_uj")
        if isinstance(domains, dict) and domains:
            readings.append({str(k): int(v) for k, v in domains.items()})
    if len(readings) < 2:
        return None
    start_uj, end_uj = readings[0], readings[-1]
    # max range unknown from samples alone; pass a large default so a single
    # wrap is absorbed (RAPL pkg counters are ~262 GJ-range, far above a run).
    max_range = {dom: 2**63 for dom in start_uj}
    joules = rapl_delta_joules(start_uj, end_uj, max_range)
    counter = (
        CounterName.RAPL_DRAM
        if all("dram" in d.lower() for d in start_uj)
        else CounterName.RAPL_PKG
    )
    return joules, counter


def _apportioned_receipt(
    *,
    action_ref: ActionRef,
    run_id: str,
    capsule_id: str,
    node_id: str,
    joules: float,
    fraction: float,
    source: MeasurementSource,
    counters_available: list[CounterName],
    wall_clock_s: float | None,
) -> EnergyReceipt:
    """Build an honest *apportioned* (time-share) receipt.

    The figure is a measured node total shared down to this action by its
    wall-clock fraction — graded ``apportioned``, never ``measured``. The
    ``attribution_basis`` records the fraction so the share is auditable, and
    ``counters_available`` names the backing counter so the forgery guard passes.
    """
    receipt = EnergyReceipt(
        action_ref=action_ref,
        run_id=run_id,
        capsule_id=capsule_id,
        measured_joules=joules * fraction,
        measurement_source=source,
        measurement_scope=MeasurementScope.PER_NODE,
        wall_clock_s=wall_clock_s,
        confidence=Confidence.APPORTIONED,
        attribution_method=AttributionMethod.TIME_SHARE,
        attribution_basis=(
            f"time_share: {fraction:.6f} of {joules:.6f} J node total "
            f"(wall-clock fraction)"
        ),
        carbon_g_co2e=None,
        grid_intensity_g_per_kwh=None,
        carbon_accounting_mode=CarbonAccountingMode.UNKNOWN,
        hardware=Hardware(node_id=node_id, counters_available=counters_available),
        generated_at=_now_iso(),
    )
    return receipt.with_payload_hash()


def attest_capsule(
    capsule_dir: Path,
    *,
    rapl_base: Path = _RAPL_BASE,
    node_id: str | None = None,
) -> list[EnergyReceipt]:
    """Produce energy receipts for every action in the capsule.

    Dispatches to :func:`attest_capsule_with_samples`, which apportions a
    measured node total when ``energy-samples.jsonl`` is present and falls back
    to honest ``unavailable`` receipts otherwise.
    """
    return attest_capsule_with_samples(
        capsule_dir, rapl_base=rapl_base, node_id=node_id
    )


def attest_capsule_with_samples(
    capsule_dir: Path,
    *,
    rapl_base: Path = _RAPL_BASE,
    node_id: str | None = None,
) -> list[EnergyReceipt]:
    """Attribute energy to each action, apportioning from samples when present.

    When ``energy-samples.jsonl`` yields a usable node joule total, every action
    gets an ``apportioned``/``time_share`` receipt whose share is its wall-clock
    fraction of the run (even split when no action has timing). When no usable
    total exists (no samples, or all gap markers), each action gets the honest
    ``unavailable`` receipt — unchanged behaviour.
    """
    run_id = _read_run_id(capsule_dir)
    node = node_id or socket.gethostname()
    capsule_id = f"capsule:{capsule_dir.name}"

    integrated = _integrate_node_joules(capsule_dir)
    if integrated is None:
        return _unavailable_receipts(
            capsule_dir,
            rapl_base=rapl_base,
            run_id=run_id,
            node=node,
            capsule_id=capsule_id,
        )

    joules, counter = integrated
    source = (
        MeasurementSource.RAPL_DRAM
        if counter is CounterName.RAPL_DRAM
        else MeasurementSource.RAPL_PKG
    )

    actions: list[tuple[ActionRef, float | None]] = []
    for kind, record in _iter_actions(capsule_dir):
        actions.append(
            (
                ActionRef(kind=kind, id=_first_id(record)),
                _action_wall_clock_s(record),
            )
        )
    if not actions:
        return []

    total_wall = sum(w for _, w in actions if w is not None)
    n = len(actions)
    receipts: list[EnergyReceipt] = []
    for ref, wall in actions:
        if total_wall > 0:
            fraction = (wall / total_wall) if wall is not None else 0.0
        else:
            fraction = 1.0 / n
        receipts.append(
            _apportioned_receipt(
                action_ref=ref,
                run_id=run_id,
                capsule_id=capsule_id,
                node_id=node,
                joules=joules,
                fraction=fraction,
                source=source,
                counters_available=[counter],
                wall_clock_s=wall,
            )
        )
    return receipts


def _unavailable_receipts(
    capsule_dir: Path,
    *,
    rapl_base: Path,
    run_id: str,
    node: str,
    capsule_id: str,
) -> list[EnergyReceipt]:
    """Honest ``unavailable`` receipts, one per action (no node total)."""
    counters = probe_counters(rapl_base=rapl_base)
    receipts: list[EnergyReceipt] = []
    for kind, record in _iter_actions(capsule_dir):
        receipts.append(
            unavailable_receipt(
                action_ref=ActionRef(kind=kind, id=_first_id(record)),
                run_id=run_id,
                capsule_id=capsule_id,
                node_id=node,
                counters_available=counters,
            )
        )
    return receipts


def write_receipts(capsule_dir: Path, receipts: list[EnergyReceipt]) -> Path:
    path = capsule_dir / RECEIPTS_FILE
    with path.open("w") as fh:
        for receipt in receipts:
            fh.write(json.dumps(receipt.to_record(), separators=(",", ":")) + "\n")
    return path


def load_receipts(capsule_dir: Path) -> list[EnergyReceipt]:
    path = capsule_dir / RECEIPTS_FILE
    return [EnergyReceipt.model_validate(record) for record in _iter_jsonl(path)]


__all__ = [
    "RECEIPTS_FILE",
    "attest_capsule",
    "attest_capsule_with_samples",
    "load_receipts",
    "write_receipts",
]
