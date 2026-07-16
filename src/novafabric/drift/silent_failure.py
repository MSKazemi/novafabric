"""Offline silent-failure detector over sealed run outcomes (ADR-0147 D6 / NF-158).

A *silent failure* is a run that reported a terminal **success** status yet whose independent
quality signal fell below a configured threshold — the run "succeeded" operationally but the
work was poor. This detector reads two things NovaFabric already seals per run — the run-capsule
``status`` enum and a per-run quality score — and flags the disagreement.

Like the two-sample drift detectors in :mod:`novafabric.drift.detectors`, ``silent_failure`` is a
**detector observation surfaced for review, not a verdict**: it does not determine that the run
failed, and there is deliberately no failed/passed/quality_ok/verdict field. A run that *already*
reported failure/aborted is never a *silent* failure — the failure was not silent.

Quality signals are taken as **higher-is-better** (e.g. a judge score or pass rate); invert an
error-rate before passing it. This first slice runs over supplied ``{run_id, status,
quality_signal}`` records; the collector that reads status + score from sealed capsules (the
ADR-0129 offline path) is a documented follow-on — no capsule mutation, no capture-path change.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import BaseModel

#: Terminal statuses treated as "reported success" unless the caller overrides.
DEFAULT_SUCCESS_STATUSES: tuple[str, ...] = ("success",)


class SilentFailureRecord(BaseModel):
    run_id: str
    status: str
    quality_signal: float
    threshold: float
    #: Detector flag — reported success but quality below threshold. NOT a "failed" verdict.
    silent_failure: bool


class SilentFailureReport(BaseModel):
    threshold: float
    success_statuses: list[str]
    total_reported_success: int
    silent_failures: int
    records: list[SilentFailureRecord]
    # Intentionally NO failed/passed/quality_ok/verdict field — surfaces observations only.


def detect_silent_failures(
    runs: Sequence[Mapping[str, object]],
    *,
    threshold: float,
    success_statuses: Sequence[str] = DEFAULT_SUCCESS_STATUSES,
) -> SilentFailureReport:
    """Flag runs that reported success but whose quality signal fell below ``threshold``.

    Each run is ``{run_id, status, quality_signal}``. A run is a silent-failure candidate when its
    ``status`` is in ``success_statuses`` **and** its ``quality_signal`` is strictly below
    ``threshold``. Quality equal to the threshold is not "below". A run whose status is not a
    reported-success state is never flagged — its failure was not silent. Surfaces observations
    for review; it does not determine that any run failed.
    """
    success_set = set(success_statuses)
    records: list[SilentFailureRecord] = []
    total_success = 0
    flagged = 0
    for run in runs:
        run_id = run.get("run_id")
        status = run.get("status")
        if not isinstance(run_id, str) or not isinstance(status, str):
            raise ValueError("each run needs a string run_id and status")
        raw_q = run.get("quality_signal")
        if not isinstance(raw_q, (int, float)) or isinstance(raw_q, bool):
            raise ValueError(f"run {run_id!r} is missing a numeric quality_signal")
        quality = float(raw_q)

        reported_success = status in success_set
        if reported_success:
            total_success += 1
        silent = reported_success and quality < threshold
        if silent:
            flagged += 1
        records.append(SilentFailureRecord(
            run_id=run_id,
            status=status,
            quality_signal=quality,
            threshold=threshold,
            silent_failure=silent,
        ))

    return SilentFailureReport(
        threshold=threshold,
        success_statuses=list(success_statuses),
        total_reported_success=total_success,
        silent_failures=flagged,
        records=records,
    )
