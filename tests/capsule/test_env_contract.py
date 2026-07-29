# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for the OQ-06 scheduler env-var self-check (PAR-ADR-003 condition 2)."""

from __future__ import annotations

import pytest

from novafabric.capsule.env_contract import diagnose_scheduler_env

_SCHEDULER_VARS = [
    "SLURM_JOB_ID",
    "SLURM_JOBID",
    "SLURM_EXPORT_ENV",
    "TORCHELASTIC_RUN_ID",
    "OMPI_COMM_WORLD_RANK",
    "RAY_WORLD_SIZE",
    "KUBERNETES_SERVICE_HOST",
    "NOVAFABRIC_GLOBAL_RUN_ID",
]


@pytest.fixture(autouse=True)
def _clean_scheduler_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _SCHEDULER_VARS:
        monkeypatch.delenv(var, raising=False)


def test_no_scheduler_detected_is_ok() -> None:
    diagnosis = diagnose_scheduler_env()
    assert diagnosis.scheduler_detected is None
    assert diagnosis.ok is True
    assert diagnosis.issues == []


def test_scheduler_and_contract_vars_both_present_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", "01ARZ3NDEKTSV4RRFFQ69G5FAV")

    diagnosis = diagnose_scheduler_env()

    assert diagnosis.scheduler_detected == "slurm"
    assert diagnosis.contract_vars_present is True
    assert diagnosis.ok is True


def test_slurm_export_none_flags_site_policy_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.setenv("SLURM_EXPORT_ENV", "NONE")

    diagnosis = diagnose_scheduler_env()

    assert diagnosis.scheduler_detected == "slurm"
    assert diagnosis.contract_vars_present is False
    assert diagnosis.ok is False
    assert diagnosis.slurm_export_env == "NONE"
    assert any("SLURM_EXPORT_ENV" in issue for issue in diagnosis.issues)
    assert any("export=ALL" in hint for hint in diagnosis.remediation)


def test_slurm_export_all_flags_submission_script_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.setenv("SLURM_EXPORT_ENV", "ALL")

    diagnosis = diagnose_scheduler_env()

    assert diagnosis.ok is False
    assert any("submission-script gap" in hint for hint in diagnosis.remediation)


def test_torchrun_detected_without_contract_vars_is_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TORCHELASTIC_RUN_ID", "abc123")

    diagnosis = diagnose_scheduler_env()

    assert diagnosis.scheduler_detected == "torchrun"
    assert diagnosis.ok is False
    assert diagnosis.slurm_export_env is None
    assert any("torchrun" in hint for hint in diagnosis.remediation)


def test_k8s_job_detected_without_contract_vars_is_a_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    diagnosis = diagnose_scheduler_env()

    assert diagnosis.scheduler_detected == "k8s_job"
    assert diagnosis.ok is False
