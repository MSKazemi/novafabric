from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from novafabric.evals._loader import load_eval_suite
from novafabric.evals.exceptions import EvalSuiteError
from novafabric.evals.result import EvalResult, Metric


def _make_mock_adapter(suite_id: str) -> Any:
    """Build a mock adapter class whose instance passes isinstance(EvalSuiteAdapter)."""
    from novafabric.evals.adapter import EvalSuiteAdapter

    class _MockAdapter:
        def suite_id(self) -> str:
            return suite_id

        def version(self) -> str:
            return "0.1.0"

        def oci_digest(self) -> str:
            return ""

        def run(self, capsule_path: Path, config: dict[str, str]) -> EvalResult:
            return EvalResult(
                suite_id=suite_id,
                suite_version="0.1.0",
                oci_digest="",
                run_at=datetime.now(timezone.utc),
                capsule_id="test-capsule",
                passed=True,
                metrics=[Metric(name="ok", value=1.0, unit="score", threshold=1.0, passed=True)],
            )

    # Verify it actually satisfies the protocol at test-collection time
    assert isinstance(_MockAdapter(), EvalSuiteAdapter)
    return _MockAdapter


def test_load_eval_suite_finds_registered_adapter() -> None:
    mock_adapter_cls = _make_mock_adapter("test-suite-v1")
    mock_ep = MagicMock()
    mock_ep.load.return_value = mock_adapter_cls

    with patch("novafabric.evals._loader.entry_points", return_value=[mock_ep]):
        adapter = load_eval_suite("test-suite-v1")

    assert adapter.suite_id() == "test-suite-v1"


def test_load_eval_suite_raises_for_unknown_suite_id() -> None:
    with patch("novafabric.evals._loader.entry_points", return_value=[]):
        with pytest.raises(EvalSuiteError, match="test-suite-v999"):
            load_eval_suite("test-suite-v999")


def test_load_eval_suite_raises_if_loaded_object_does_not_conform() -> None:
    """An entry point that loads a plain object not conforming to EvalSuiteAdapter raises."""

    class _BadAdapter:
        # Missing required protocol methods
        def suite_id(self) -> str:
            return "bad-suite"

        # Missing: version(), oci_digest(), run()

    mock_ep = MagicMock()
    mock_ep.name = "bad-suite"
    # Return an instance directly (not a class) so callable() check skips instantiation,
    # but the instance fails isinstance(EvalSuiteAdapter)
    mock_ep.load.return_value = _BadAdapter()

    with patch("novafabric.evals._loader.entry_points", return_value=[mock_ep]):
        with pytest.raises(EvalSuiteError, match="does not conform"):
            load_eval_suite("bad-suite")


def test_load_eval_suite_skips_non_matching_suite_ids() -> None:
    """If the adapter's suite_id() does not match, keep searching."""
    mock_adapter_cls = _make_mock_adapter("other-suite-v1")
    mock_ep = MagicMock()
    mock_ep.load.return_value = mock_adapter_cls

    with patch("novafabric.evals._loader.entry_points", return_value=[mock_ep]):
        with pytest.raises(EvalSuiteError, match="no eval suite adapter registered"):
            load_eval_suite("requested-suite-v1")


def test_load_all_three_adapters() -> None:
    """Each of the three E-5 adapters must be discoverable via the entry-point loader.

    This test uses the real entry-point registry (populated by ``pip install -e .``),
    so it validates that the ``pyproject.toml`` entries are correct and the classes
    are importable.
    """
    from novafabric.evals.suites.agent_bench import AgentBenchAdapter
    from novafabric.evals.suites.gaia import GaiaAdapter
    from novafabric.evals.suites.swe_bench import SweBenchAdapter

    gaia = load_eval_suite("gaia-v1")
    assert isinstance(gaia, GaiaAdapter)
    assert gaia.suite_id() == "gaia-v1"

    swe = load_eval_suite("swe-bench-verified-v1")
    assert isinstance(swe, SweBenchAdapter)
    assert swe.suite_id() == "swe-bench-verified-v1"

    ab = load_eval_suite("agentbench-v1")
    assert isinstance(ab, AgentBenchAdapter)
    assert ab.suite_id() == "agentbench-v1"
