"""Packaging/distribution-metadata tests.

Named ``packaging_metadata`` and *not* ``packaging`` on purpose. ``pythonpath``
in ``pyproject.toml`` includes ``tests``, so a test package here registers as a
top-level importable name for the whole pytest session. ``tests/packaging/``
therefore shadowed the installed ``packaging`` distribution: ``packaging.version``
became unimportable and ``import presidio_analyzer`` — fine outside pytest —
raised ``ModuleNotFoundError`` inside it.

``tests/docs/test_test_layout.py`` now asserts this class of collision away.
"""
