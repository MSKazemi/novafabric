"""Reference capture-hook plugin (RFC-0001 Option C, sub-track C-2).

What this plugin demonstrates:

1. **Discovery.** The ``[project.entry-points."novafabric.hooks"]``
   block in ``pyproject.toml`` is what NovaFabric walks at capture
   start. Once this package is ``pip install``-ed alongside novafabric,
   the hook auto-loads — no code changes to NovaFabric required.

2. **The HookProtocol surface.** Three methods: ``__init__(writer,
   parent_span_id)``, ``install()``, ``uninstall()``. Plus an optional
   ``info()`` classmethod returning :class:`HookPluginInfo` for richer
   diagnostics.

3. **Patch a hypothetical SDK.** This reference patches a fake SDK
   defined inline (``_FakeAcmeAI``) so the example runs without any
   real third-party dependency. Real plugins replace
   ``_FakeAcmeAI.create`` with the actual SDK call site they want to
   intercept.

4. **Failure isolation.** ``install()`` returns silently if the target
   SDK is not importable, matching the built-in hooks' pattern. A
   buggy plugin must never break NovaFabric's built-in capture.

Run the live demo (after installing this package):

    nova capture python -m novafabric_example_plugin.demo
"""
from __future__ import annotations

import functools
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from novafabric.capture._ulid import new_ulid
from novafabric.capture.hooks._plugin import HookPluginInfo

if TYPE_CHECKING:
    from novafabric.capture.capsule import CapsuleWriter


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# ── Fake "AcmeAI" SDK — what a real plugin's target would be ─────────────────
#
# A real plugin patches a real third-party SDK. To keep this reference
# self-contained and runnable in CI without network access, we define
# a tiny fake SDK module-locally and patch THAT. The shape mirrors how
# you'd patch e.g. `cohere.Client.chat` or any other call site.


class _FakeAcmeAI:
    """Stand-in for a real SDK class. Patched by ExampleHook."""

    @staticmethod
    def create(model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
        # In a real SDK this would round-trip to the provider. Here it
        # just returns a deterministic stub so the demo is reproducible.
        return {
            "id": "acme-resp-0",
            "model": model,
            "completion": f"(fake) echo of: {prompt[:40]}",
            "usage": {"input_tokens": 8, "output_tokens": 12},
        }


# ── The plugin hook ──────────────────────────────────────────────────────────


class ExampleHook:
    """Reference plugin hook satisfying NovaFabric's HookProtocol."""

    def __init__(self, writer: "CapsuleWriter", parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._original: Any = None  # original method, restored on uninstall

    @classmethod
    def info(cls) -> HookPluginInfo:
        """Optional metadata accessor for diagnostics. NovaFabric's plugin
        loader calls this if present and uses the returned values in
        log messages and the capsule's plugin manifest."""
        return HookPluginInfo(
            name="acme-ai-reference",
            version="0.1.0",
            capabilities=("model_calls",),
        )

    # The two required HookProtocol methods.

    def install(self) -> None:
        """Patch the target SDK's call site. Must not raise."""
        try:
            # In a real plugin: `import acme_ai`. We reach into our
            # locally-defined fake to keep the example self-contained.
            target_class = _FakeAcmeAI
            self._original = target_class.create
            hook_self = self

            @functools.wraps(self._original)
            def patched(model: str, prompt: str, **kwargs: Any) -> dict[str, Any]:
                return hook_self._intercept(model, prompt, **kwargs)

            target_class.create = patched  # type: ignore[method-assign]
        except (ImportError, AttributeError):
            # Target SDK not available — no-op, exactly as the built-in
            # hooks do. Capture continues with the rest of the hooks.
            pass

    def uninstall(self) -> None:
        """Reverse install(). Must be idempotent."""
        if self._original is None:
            return
        try:
            _FakeAcmeAI.create = self._original  # type: ignore[method-assign]
        except (ImportError, AttributeError):
            pass
        finally:
            self._original = None

    # ── Recording ────────────────────────────────────────────────────────────

    def _intercept(
        self, model: str, prompt: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Wrap the original call and persist a model-calls.jsonl record."""
        started = _now()
        t0 = time.monotonic()
        try:
            response = self._original(model, prompt, **kwargs)
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._record(started, _now(), duration_ms, model, prompt, response)
            return response  # type: ignore[no-any-return]
        except Exception as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            self._record_error(started, _now(), duration_ms, model, prompt, exc)
            raise

    def _record(
        self, started: str, finished: str, duration_ms: int,
        model: str, prompt: str, response: dict[str, Any],
    ) -> None:
        usage = response.get("usage") or {}
        # Mirror the OTel GenAI semconv field shape used by the built-in
        # hooks. A plugin records into the same model-calls.jsonl as
        # everything else; the capsule's reader doesn't know (or care)
        # which hook produced the record.
        self._writer.append_model_call({
            "schema_version": "0.1.0",
            "semconv_version": "1.30.0",
            "model_call_id": new_ulid(),
            "parent_span_id": self._parent_span_id,
            "started_at": started,
            "finished_at": finished,
            "duration_ms": duration_ms,
            "gen_ai.system": "acme-ai",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
            "gen_ai.response.model": response.get("model", model),
            "gen_ai.request.messages": [{"role": "user", "content": prompt}],
            "gen_ai.response.choices": [{
                "index": 0,
                "message": {"role": "assistant",
                            "content": response.get("completion", "")},
                "finish_reason": "stop",
            }],
            "gen_ai.usage.input_tokens": int(usage.get("input_tokens", 0) or 0),
            "gen_ai.usage.output_tokens": int(usage.get("output_tokens", 0) or 0),
            "status": "success",
            "extensions": {
                # Plugins get their own slot under `extensions` so the
                # record format stays canonical and consumers can attribute
                # records to their producer.
                "io.novafabric.capture_method": "plugin",
                "io.novafabric.plugin_name": "acme-ai-reference",
            },
        })

    def _record_error(
        self, started: str, finished: str, duration_ms: int,
        model: str, prompt: str, exc: Exception,
    ) -> None:
        self._writer.append_model_call({
            "schema_version": "0.1.0",
            "semconv_version": "1.30.0",
            "model_call_id": new_ulid(),
            "parent_span_id": self._parent_span_id,
            "started_at": started,
            "finished_at": finished,
            "duration_ms": duration_ms,
            "gen_ai.system": "acme-ai",
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
            "gen_ai.response.model": model,
            "gen_ai.request.messages": [{"role": "user", "content": prompt}],
            "gen_ai.response.choices": [],
            "gen_ai.usage.input_tokens": 0,
            "gen_ai.usage.output_tokens": 0,
            "status": "error",
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback_ref": None,
            },
            "extensions": {
                "io.novafabric.capture_method": "plugin",
                "io.novafabric.plugin_name": "acme-ai-reference",
            },
        })


# Re-export the fake SDK so the demo / tests can call it.
fake_acme_ai = _FakeAcmeAI
