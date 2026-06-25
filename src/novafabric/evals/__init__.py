from __future__ import annotations

from novafabric.evals._loader import load_eval_suite
from novafabric.evals._stats import MetricComparison, RegressionDetector, RegressionReport
from novafabric.evals.adapter import EvalSuiteAdapter
from novafabric.evals.exceptions import EvalSuiteError
from novafabric.evals.result import EvalResult, Metric, StatisticalContext

__all__ = [
    "EvalSuiteAdapter",
    "EvalResult",
    "Metric",
    "MetricComparison",
    "RegressionDetector",
    "RegressionReport",
    "StatisticalContext",
    "EvalSuiteError",
    "load_eval_suite",
]
