from __future__ import annotations

from importlib.metadata import entry_points

from novafabric.evals.adapter import EvalSuiteAdapter
from novafabric.evals.exceptions import EvalSuiteError

_ENTRY_POINT_GROUP = "novafabric.eval_suites"


def load_eval_suite(suite_id: str) -> EvalSuiteAdapter:
    """Discover and return the registered adapter for *suite_id*.

    Searches the ``novafabric.eval_suites`` entry-point group.  Each entry
    point is loaded and instantiated (if it is a class); the first whose
    :meth:`~EvalSuiteAdapter.suite_id` matches ``suite_id`` is returned.

    Raises
    ------
    EvalSuiteError
        If no adapter is registered for the given ``suite_id``, or if the
        loaded object does not conform to :class:`EvalSuiteAdapter`.
    """
    eps = entry_points(group=_ENTRY_POINT_GROUP)
    for ep in eps:
        raw = ep.load()
        # Support both class-based and instance-based adapters.
        instance = raw() if callable(raw) else raw
        if not isinstance(instance, EvalSuiteAdapter):
            raise EvalSuiteError(
                f"entry point {ep.name!r} loaded object {raw!r} does not "
                "conform to EvalSuiteAdapter"
            )
        if instance.suite_id() == suite_id:
            return instance
    raise EvalSuiteError(
        f"no eval suite adapter registered for suite_id={suite_id!r}"
    )
