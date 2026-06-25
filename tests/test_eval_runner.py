from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.eval.runner import run_evals
from novafabric.registry.service import (
    PromotionBlockedError,
    promote_asset,
    register_asset,
)
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec


@pytest.fixture
def registered_agent(tmp_db: Path, valid_agent_yaml: Path) -> dict:  # type: ignore[type-arg]
    spec = validate_spec(valid_agent_yaml)
    return register_asset(spec, valid_agent_yaml, db_path=tmp_db)


def test_eval_suite_not_found_records_failure(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    with patch("novafabric.eval.runner.entry_points", return_value=[]):
        result = run_evals("kube-rca-agent", "v1.0.0", db_path=tmp_db)
    assert result["passed"] is False
    suites = result["suites"]
    assert len(suites) == 1
    assert suites[0]["suite_name"] == "basic_rca_suite"
    assert "not registered" in suites[0]["reason"].lower()


def test_eval_suite_found_and_passes(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    mock_ep = MagicMock()
    mock_ep.name = "basic_rca_suite"
    mock_fn = MagicMock(return_value={"passed": True, "score": 0.95})
    mock_ep.load.return_value = mock_fn

    with patch("novafabric.eval.runner.entry_points", return_value=[mock_ep]):
        result = run_evals("kube-rca-agent", "v1.0.0", db_path=tmp_db)

    assert result["passed"] is True
    assert result["suites"][0]["passed"] is True


def test_eval_result_stored_in_db(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    from novafabric.registry.store import get_connection, init_schema

    with patch("novafabric.eval.runner.entry_points", return_value=[]):
        run_evals("kube-rca-agent", "v1.0.0", db_path=tmp_db)

    conn = get_connection(tmp_db)
    init_schema(conn)
    rows = conn.execute("SELECT * FROM eval_results").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["passed"] == 0


def test_promotion_blocked_before_eval(
    tmp_db: Path, valid_agent_yaml: Path
) -> None:
    spec = validate_spec(valid_agent_yaml)
    register_asset(spec, valid_agent_yaml, db_path=tmp_db)
    with pytest.raises(PromotionBlockedError):
        promote_asset(
            "kube-rca-agent", "v1.0.0", AssetStatus.staging, "tester", db_path=tmp_db
        )


def test_promotion_allowed_after_passing_eval(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    mock_ep = MagicMock()
    mock_ep.name = "basic_rca_suite"
    mock_ep.load.return_value = MagicMock(return_value={"passed": True})

    with patch("novafabric.eval.runner.entry_points", return_value=[mock_ep]):
        run_evals("kube-rca-agent", "v1.0.0", db_path=tmp_db)

    result = promote_asset(
        "kube-rca-agent", "v1.0.0", AssetStatus.staging, "tester", db_path=tmp_db
    )
    assert result["status"] == "staging"
