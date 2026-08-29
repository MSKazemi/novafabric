"""NovaFabric adapter for Haystack.

Wraps a ``haystack.Pipeline`` so that each ``run()`` writes a Run Capsule.
Wire-level hooks capture the model calls its components make.

Haystack is an **optional** dependency — this module must stay importable
without it; :func:`wrap_pipeline` raises :class:`ImportError` at call time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from novafabric.adapters._capsule import begin_capture, require


def wrap_pipeline(
    pipeline: Any,
    *,
    run_name: str | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Wrap a Haystack ``Pipeline`` for NovaFabric capture.

    Patches ``run`` in place and returns the same object.

    Both ``Pipeline.run`` and ``AsyncPipeline.run_async`` are patched when
    present, because Haystack exposes the async variant as a separate method
    rather than as a coroutine returned by ``run``.

    Args:
        pipeline: A ``haystack.Pipeline`` (or ``AsyncPipeline``) instance.
        run_name: Name recorded in the manifest. Defaults to the class name.
        data_dir: Base directory for capsules.

    Raises:
        ImportError: If ``haystack-ai`` is not installed.

    Usage::

        from novafabric.adapters.haystack import wrap_pipeline
        pipe = wrap_pipeline(pipe, run_name="rag-qa")
        result = pipe.run({"retriever": {"query": "..."}})
    """
    require("haystack", "haystack-ai")

    resolved_name = run_name or type(pipeline).__name__ or "haystack-run"

    def _capture() -> Any:
        return begin_capture(
            framework="haystack", run_name=resolved_name, data_dir=data_dir
        )

    if hasattr(pipeline, "run"):
        original_run = pipeline.run

        def wrapped_run(*args: Any, **kwargs: Any) -> Any:
            cap = _capture()
            cap.tags["entry_point"] = "run"
            try:
                return original_run(*args, **kwargs)
            except Exception as exc:
                cap.fail(exc)
                raise
            finally:
                cap.finish()

        pipeline.run = wrapped_run

    if hasattr(pipeline, "run_async"):
        original_run_async = pipeline.run_async

        async def wrapped_run_async(*args: Any, **kwargs: Any) -> Any:
            cap = _capture()
            cap.tags["entry_point"] = "run_async"
            try:
                return await original_run_async(*args, **kwargs)
            except Exception as exc:
                cap.fail(exc)
                raise
            finally:
                cap.finish()

        pipeline.run_async = wrapped_run_async

    return pipeline
