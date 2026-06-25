from __future__ import annotations


class EvalSuiteError(Exception):
    """Raised when a suite adapter fails to execute.

    This is distinct from the capsule *failing the benchmark* — that is
    expressed via ``EvalResult.passed = False``.  ``EvalSuiteError`` signals
    an infrastructure-level failure (adapter not found, adapter crashed, etc.).
    """
