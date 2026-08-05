# Writing a NovaFabric capture-hook plugin

> **Status: experimental.** Introduced in v0.5.x with a v0.7 stability
> checkpoint planned (contingent on a third-party plugin being published and
> used in real workflows — see
> RFC-0001 §"Adoption / migration plan").
> As of this writing (repo at v0.96.0) no ADR or release note records that
> stability checkpoint having been formally revisited or declared met — the
> in-tree reference implementation (`examples/plugin-hook-reference/`) is
> NovaFabric's own example, not independent third-party evidence. Treat the
> contract as **still experimental** and pin your plugin's `novafabric`
> dependency accordingly until a stability decision is recorded.

> **Working reference:** `examples/plugin-hook-reference/` is a complete,
> installable Python package that demonstrates everything below
> end-to-end. Clone it as a starting template:
>
> ```bash
> cp -r examples/plugin-hook-reference my-novafabric-plugin
> uv pip install -e my-novafabric-plugin/
> nova capture python -m novafabric_example_plugin.demo
> # → captured record carries:
> #   "io.novafabric.capture_method": "plugin"
> #   "io.novafabric.plugin_name": "acme-ai-reference"
> ```
>
> The doc below explains each piece. The reference proves they fit
> together.

---

## Why plugins exist

NovaFabric's capture surface ships with hooks for a small set of SDKs and protocols (OpenAI, Anthropic, MCP, plus the wire-level layer over `httpx` and `requests`). Per RFC-0001 Option C, per-SDK hooks are no longer the project's primary growth axis — wire-level capture and OTel-native ingest carry that load. But some SDKs cannot be reached from those layers (framework-specific control flow, in-process state graphs that never hit the wire).

Plugins are the long tail's path. You publish a Python package that exposes a hook; NovaFabric discovers it at capture time and installs it alongside the built-ins. NovaFabric does not maintain the plugin, ship it, or vouch for it.

---

## Minimum viable plugin

A plugin is two files: a hook class and a `pyproject.toml` entry-point declaration.

### The hook class

```python
# my_plugin/hook.py
from typing import Any

from novafabric.capture.hooks._plugin import HookPluginInfo


class AcmeHook:
    """Capture hook for the (fictional) Acme agent SDK."""

    def __init__(self, writer: Any, parent_span_id: str) -> None:
        self._writer = writer
        self._parent_span_id = parent_span_id
        self._original = None

    def install(self) -> None:
        try:
            import acme_sdk
            self._original = acme_sdk.Client.run
            hook_self = self

            def patched_run(client_self, *args, **kwargs):
                # ... inspect arguments, call original, record to writer ...
                return hook_self._original(client_self, *args, **kwargs)

            acme_sdk.Client.run = patched_run
        except ImportError:
            # SDK absent at runtime → silent no-op. Identical pattern to
            # the built-in hooks (capture/hooks/_openai.py et al.).
            pass

    def uninstall(self) -> None:
        if self._original is None:
            return
        try:
            import acme_sdk
            acme_sdk.Client.run = self._original
        finally:
            self._original = None

    @classmethod
    def info(cls) -> HookPluginInfo:
        # Optional. Used by NovaFabric for log/diagnostic output.
        return HookPluginInfo(
            name="novafabric-acme",
            version="0.1.0",
            capabilities=("model-calls",),
        )
```

### The entry-point declaration

```toml
# pyproject.toml of the plugin's package
[project]
name = "novafabric-acme"
version = "0.1.0"

[project.entry-points."novafabric.hooks"]
acme = "my_plugin.hook:AcmeHook"
```

After `pip install novafabric-acme`, NovaFabric's `install_all` discovers `acme` in the `novafabric.hooks` entry-point group and installs it alongside the built-ins.

---

## The contract

The full contract is [`HookProtocol` in `_plugin.py`](../../src/novafabric/capture/hooks/_plugin.py). Short form:

| Method | Required | Purpose |
|---|---|---|
| `__init__(writer, parent_span_id)` | yes | Receive the active capsule writer and parent OTel span id. Plugin code stores them. |
| `install()` | yes | Patch the target SDK or library. **Must not raise** — failures should be caught internally and leave the runtime in a clean state. |
| `uninstall()` | yes | Reverse `install()`. **Must be idempotent** and safe to call when `install()` was never called or failed. |
| `info()` | no | Return a `HookPluginInfo`. NovaFabric calls this on the *class* (use `@classmethod` or `@staticmethod`). Used only for logs. Plugins without it still work. |

The `writer` argument is a `novafabric.capture.capsule.CapsuleWriter`. The two methods plugins should use:

```python
self._writer.append_model_call({...})  # for LLM calls
self._writer.append_tool_call({...})   # for tool / RPC calls
```

The record schemas are documented in `design/spec/tool-call-v0.md` and `design/spec/model-call-v0.md`. The capsule writer is thread-safe; multiple hooks (built-in + plugins) writing concurrently is supported.

**Public surface, in writing.** The two constructor arguments (`writer`, `parent_span_id`) and the three lifecycle methods are the public surface. Anything starting with `_` on the writer or hook objects is internal and may change without notice. Coupling to internals will break.

---

## Lifecycle

```
nova capture <cmd>
   └─ orchestrator opens capsule writer
      └─ install_all() called in subprocess
         ├─ Built-in hooks (OpenAI, Anthropic, MCP, requests, httpx) install
         └─ Plugins discovered + installed (one isolated try/except per plugin)
                                 ↓
            agent runs, hooks record into capsule
                                 ↓
                     uninstall_all() at exit
```

A plugin that raises in `__init__` or `install()` is logged at WARNING and skipped. Built-in capture continues. The user sees a one-line message; the run proceeds.

---

## Versioning posture

- **v0.5.x — experimental.** The contract may change in minor releases. Plugins targeting v0.5.x should pin `novafabric < 0.6` or be prepared to adjust.
- **v0.6 — experimental continues.** Breaking changes (if any) announced in release notes.
- **v0.7 — planned stability checkpoint.** If at least one third-party plugin had been published and used in real workflows by v0.7, the contract would be declared stable, after which breaking changes would require an RFC and a deprecation cycle.

**Where this stands as of v0.96.0:** the repo is well past v0.7 and no ADR or
release note in-tree records that checkpoint having been formally revisited —
this doc's own bounded-experimental-period framing was written assuming the
checkpoint would be actively re-decided at v0.7, and that re-decision does not
appear to have happened. Until a stability decision is recorded, continue to
treat the contract as **experimental** rather than assume it lapsed into
stable by default. This experimental period was meant to be bounded on
purpose — silently extending it indefinitely would push plugin authors away —
so an unresolved checkpoint like this one is itself a signal to revisit the
design via a successor RFC, not to guess at its status.

---

## Testing your plugin

The pattern NovaFabric uses internally (see `tests/test_capture_plugin_contract.py`) injects fake entry points via `unittest.mock.patch` against `importlib.metadata.entry_points`. You don't need a real `pip install` to exercise the discovery flow.

For unit-testing the hook itself, follow the in-tree examples (`tests/test_capture_hooks.py`): construct the hook with a `CapsuleWriter` pointed at a `tmp_path`, call your wrapped method, then read back `tool-calls.jsonl` or `model-calls.jsonl` and assert against the records.

---

## What plugins do **not** get

- **No special trust scope.** Plugins run with full process privileges. NovaFabric does not sandbox them. Users install plugins through `pip` and trust them the same way they trust any other Python dependency. (This maps to the E-2 threat in the project's threat model: untrusted code at runtime is the user's responsibility.)
- **No backwards-compatibility shims at the experimental stage.** A breaking change between v0.5.x and v0.6 won't ship a translation layer. Pin your dependency and read the release notes.
- **No plugin manager.** There is no `nova plugins list / disable / enable`. Discovery is implicit through `pip install`. If you need to disable a plugin, uninstall its package.
- **No vetting / curation.** NovaFabric doesn't review plugins. The contract is the only quality gate.

If any of these become real adoption blockers, the relevant constraint will be revisited in a successor RFC. For now, the plugin path is *minimum viable*.

---

## Where to look

- The contract: [`src/novafabric/capture/hooks/_plugin.py`](../../src/novafabric/capture/hooks/_plugin.py)
- A reference hook (built-in, but the same shape applies): [`src/novafabric/capture/hooks/_openai.py`](../../src/novafabric/capture/hooks/_openai.py)
- Discovery + lifecycle tests: [`tests/test_capture_plugin_contract.py`](../../tests/test_capture_plugin_contract.py)
- Strategic context: RFC-0001 §Detailed design — Option C
