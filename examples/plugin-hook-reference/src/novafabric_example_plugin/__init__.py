"""NovaFabric capture-hook plugin reference (C-2 contract).

This is a working, installable example. Real plugins follow the same
shape: a hook class implementing the install/uninstall contract,
declared under the ``novafabric.hooks`` entry-point group in
``pyproject.toml``.
"""

from novafabric_example_plugin.hook import ExampleHook

__all__ = ["ExampleHook"]
