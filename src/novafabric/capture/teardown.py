"""Fail-open teardown for in-process captures (``@agent`` and the framework adapters).

An in-process capture writes its capsule in the user's own call path, usually
from a ``finally:``. Anything that raises there — ENOSPC writing ``env.lock``, a
link planted in the capsule, a source checkout with no package metadata —
replaced the workload's own return value or exception with NovaFabric's (review
finding eval-adapters-query-04), breaking "capture never blocks the user
workload by default".

:class:`TeardownSteps` runs each step in a guard. A step that fails is logged
and counted (:func:`~novafabric.capture.hooks._fail_open.note_capture_failure`),
its result falls back to a default, and the failure is collected into
:attr:`TeardownSteps.incomplete_reason`. Hand that to
:func:`~novafabric.capture.finalize.finalize_in_process_capsule`: the capsule is
still scanned and redacted — skipping finalization would leave raw secrets on
disk — but it is never sealed, and ``metadata.finalization_error`` says which
step failed. Fail-open, never fail-silent.
"""
from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import TypeVar

from novafabric.capture.hooks._fail_open import note_capture_failure

T = TypeVar("T")


class TeardownSteps:
    """Run teardown steps for one capture; never raises from :meth:`run`."""

    def __init__(self, hook: str) -> None:
        self.hook = hook
        self.failed: list[str] = []

    def run(self, label: str, action: Callable[[], T], default: T) -> T:
        """``action()``, or *default* when it raises (recorded under *label*)."""
        try:
            return action()
        except Exception as exc:
            note_capture_failure(self.hook, label, exc)
            self.failed.append(f"{label}: {type(exc).__name__}")
            return default

    def do(self, label: str, action: Callable[[], object]) -> None:
        """:meth:`run` for a step whose result is not needed."""
        self.run(label, action, None)

    @property
    def incomplete_reason(self) -> str | None:
        """Why the capsule must not be sealed, or ``None`` when every step succeeded."""
        return f"capture incomplete: {', '.join(self.failed)}" if self.failed else None


def novafabric_version() -> str:
    """The installed version, or ``unknown`` for a source tree run without an install."""
    try:
        return _pkg_version("novafabric")
    except PackageNotFoundError:
        return "unknown"
