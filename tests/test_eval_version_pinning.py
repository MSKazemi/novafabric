"""Tests for versioned eval provenance (ADR-0085, gap-014).

Eval results must carry the asset version and (optionally) the dataset version
so a result can be tied back to exactly what was measured.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.eval.runner import run_evals
from novafabric.registry.service import register_asset
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.validator import validate_spec


@pytest.fixture
def registered_agent(tmp_db: Path, valid_agent_yaml: Path) -> dict:  # type: ignore[type-arg]
    spec = validate_spec(valid_agent_yaml)
    return register_asset(spec, valid_agent_yaml, db_path=tmp_db)


def _run(tmp_db: Path, dataset_version: str | None) -> dict:  # type: ignore[type-arg]
    mock_ep = MagicMock()
    mock_ep.name = "basic_rca_suite"
    mock_ep.load.return_value = MagicMock(return_value={"passed": True, "score": 0.9})
    with patch("novafabric.eval.runner.entry_points", return_value=[mock_ep]):
        return run_evals(
            "kube-rca-agent", "v1.0.0", db_path=tmp_db, dataset_version=dataset_version
        )


def test_asset_version_always_pinned(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    result = _run(tmp_db, dataset_version=None)
    suite = result["suites"][0]
    assert suite["asset_version"] == "v1.0.0"


def test_dataset_version_pinned_when_provided(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    result = _run(tmp_db, dataset_version="gaia-2024-06")
    suite = result["suites"][0]
    assert suite["dataset_version"] == "gaia-2024-06"
    assert suite["asset_version"] == "v1.0.0"


def test_dataset_version_omitted_cleanly_when_absent(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    result = _run(tmp_db, dataset_version=None)
    suite = result["suites"][0]
    # Omitted cleanly from the convenience return shape when not provided.
    assert "dataset_version" not in suite


def test_versions_persisted_in_score_json(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    _run(tmp_db, dataset_version="gaia-2024-06")
    conn = get_connection(tmp_db)
    init_schema(conn)
    rows = conn.execute("SELECT score_json FROM eval_results").fetchall()
    conn.close()
    assert len(rows) == 1
    blob = json.loads(rows[0]["score_json"])
    assert blob["asset_version"] == "v1.0.0"
    assert blob["dataset_version"] == "gaia-2024-06"
    assert blob["score"] == 0.9


def test_dataset_version_null_in_score_json_when_absent(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    _run(tmp_db, dataset_version=None)
    conn = get_connection(tmp_db)
    init_schema(conn)
    blob = json.loads(
        conn.execute("SELECT score_json FROM eval_results").fetchone()["score_json"]
    )
    conn.close()
    assert blob["dataset_version"] is None
    assert blob["asset_version"] == "v1.0.0"


def test_backward_compatible_default_call(
    tmp_db: Path, registered_agent: dict  # type: ignore[type-arg]
) -> None:
    # Calling without the new kwarg must still work (backward compatible).
    mock_ep = MagicMock()
    mock_ep.name = "basic_rca_suite"
    mock_ep.load.return_value = MagicMock(return_value={"passed": True})
    with patch("novafabric.eval.runner.entry_points", return_value=[mock_ep]):
        result = run_evals("kube-rca-agent", "v1.0.0", db_path=tmp_db)
    assert result["passed"] is True
    assert result["suites"][0]["asset_version"] == "v1.0.0"
