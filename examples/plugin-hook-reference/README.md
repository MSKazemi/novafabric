# NovaFabric capture-hook plugin reference

**Use this when you want to:** write your own NovaFabric capture-hook
plugin. This is a complete, installable Python package that
demonstrates the `novafabric.hooks` entry-point contract end-to-end.

## What's in here

```
plugin-hook-reference/
├── pyproject.toml                              # entry-point declaration
├── src/novafabric_example_plugin/
│   ├── __init__.py
│   ├── hook.py                                 # the hook class
│   └── demo.py                                 # workload to run under capture
└── tests/
    └── test_hook.py                            # full unit tests
```

The hook patches a fake `AcmeAI` SDK defined inline in `hook.py` so the
example runs without any real third-party dependency. In a real plugin,
you'd patch the actual SDK call site you want to intercept (e.g.
`cohere.Client.chat`, `openai.beta.threads.runs.create`, anything that
NovaFabric doesn't already cover via its built-in hooks).

## Run it locally

```bash
# Install the plugin alongside novafabric (editable so you can iterate):
uv pip install -e examples/plugin-hook-reference/

# Capture the demo workload — the hook auto-discovers and fires:
nova capture python -m novafabric_example_plugin.demo

# Inspect the captured record (look for io.novafabric.plugin_name):
RUN=$(ls -dt .novafabric/runs/*/ | head -1)
jq '.extensions, .["gen_ai.system"], .["gen_ai.request.model"]' \
   $RUN/model-calls.jsonl
```

You should see the captured record's `extensions` block contain
`{"io.novafabric.capture_method": "plugin", "io.novafabric.plugin_name":
"acme-ai-reference"}` — proof the plugin fired alongside NovaFabric's
built-ins.

## Run the tests

```bash
cd examples/plugin-hook-reference
uv run --with pytest pytest -v
```

Six tests pass: protocol satisfaction, `info()` metadata, install/
uninstall round-trip, idempotency, captured-call recording, error path.

## Use it as a template for your own plugin

```bash
cp -r examples/plugin-hook-reference my-novafabric-plugin
cd my-novafabric-plugin

# 1. Rename the Python package to your namespace:
mv src/novafabric_example_plugin src/novafabric_<your_name>

# 2. Edit pyproject.toml:
#    - [project] name = "novafabric-<your-name>"
#    - [project.entry-points."novafabric.hooks"]
#      <your-key> = "novafabric_<your_name>.hook:YourHook"

# 3. Edit src/novafabric_<your_name>/hook.py:
#    - Rename ExampleHook -> YourHook
#    - Replace the _FakeAcmeAI patch site with your real SDK
#    - Update info() metadata
#    - Adjust the gen_ai.system value in the recorded record

# 4. Update tests/

# 5. uv pip install -e .

# 6. nova capture python your_workload.py
```

## Where the contract is defined

- [`docs/integrations/writing-a-hook-plugin.md`](../../docs/integrations/writing-a-hook-plugin.md)
  — the plugin author manual
- [`src/novafabric/capture/hooks/_plugin.py`](../../src/novafabric/capture/hooks/_plugin.py)
  — `HookProtocol`, `HookPluginInfo`, the entry-point group name, and
  the discovery / failure-isolation behavior
- `design/governance/RFC-0001-multi-vendor-strategy.md` (private — maintainers only)
  §"Detailed design — Option C, sub-track C-2" — the strategic
  rationale

## Status

The `HookProtocol` surface is **experimental in v0.5.x and v0.6.x**.
Stability is queued for v0.7. This reference is updated alongside the
contract; clone at the v0.6.x tag if you need a stable reference for
that minor version.
