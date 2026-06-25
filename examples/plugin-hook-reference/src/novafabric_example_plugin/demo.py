"""Tiny workload that exercises the reference plugin's target SDK.

Run under ``nova capture`` once this package is installed::

    uv pip install -e examples/plugin-hook-reference/
    nova capture python -m novafabric_example_plugin.demo

The captured ``model-calls.jsonl`` will contain one record produced by
the plugin (look for ``"io.novafabric.plugin_name": "acme-ai-reference"``
under ``extensions``).
"""
from __future__ import annotations

import json

from novafabric_example_plugin.hook import fake_acme_ai


def main() -> None:
    response = fake_acme_ai.create(
        model="acme-large-v1",
        prompt="Hello from the reference plugin's demo workload.",
        temperature=0.7,
    )
    print("acme-ai response:", json.dumps(response, indent=2))


if __name__ == "__main__":
    main()
