from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from novafabric.evals.result import EvalResult, Metric, StatisticalContext


@pytest.fixture
def valid_fixture() -> dict:  # type: ignore[type-arg]
    fixtures_dir = Path(__file__).parent.parent / "fixtures"
    return json.loads((fixtures_dir / "eval_result_valid.json").read_text())


def test_eval_result_validates_with_valid_fixture(valid_fixture: dict) -> None:  # type: ignore[type-arg]
    result = EvalResult.model_validate(valid_fixture)
    assert result.suite_id == "gaia-level-1"
    assert result.passed is True
    assert len(result.metrics) == 2


def test_p_value_out_of_range_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="p_value must be in"):
        StatisticalContext(p_value=1.5)


def test_p_value_negative_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="p_value must be in"):
        StatisticalContext(p_value=-0.01)


def test_p_value_boundary_values_accepted() -> None:
    ctx0 = StatisticalContext(p_value=0.0)
    assert ctx0.p_value == 0.0
    ctx1 = StatisticalContext(p_value=1.0)
    assert ctx1.p_value == 1.0


def test_metrics_empty_list_raises_validation_error(valid_fixture: dict) -> None:  # type: ignore[type-arg]
    valid_fixture["metrics"] = []
    with pytest.raises(ValidationError, match="at least one"):
        EvalResult.model_validate(valid_fixture)


def test_confidence_interval_wrong_length_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="exactly 2 elements"):
        StatisticalContext(confidence_interval=[0.1, 0.5, 0.9])


def test_confidence_interval_single_element_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match="exactly 2 elements"):
        StatisticalContext(confidence_interval=[0.5])


def test_confidence_interval_two_elements_accepted() -> None:
    ctx = StatisticalContext(confidence_interval=[0.1, 0.9])
    assert ctx.confidence_interval == [0.1, 0.9]


def test_metric_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        Metric(name="x", value=1.0, unit="score", threshold=1.0, passed=True, extra="nope")


def test_eval_result_extra_fields_forbidden(valid_fixture: dict) -> None:  # type: ignore[type-arg]
    valid_fixture["unexpected_field"] = "bad"
    with pytest.raises(ValidationError):
        EvalResult.model_validate(valid_fixture)
