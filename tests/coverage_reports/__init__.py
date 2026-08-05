"""Test package marker — see tests/capsule/__init__.py.

Without this, pytest imports test modules by bare basename, so two files
named the same in different directories collide at collection. That is not
hypothetical: tests/embodied/ and tests/federation/ both shipped a
test_facet.py and broke collection outright.

Named ``coverage_reports`` and *not* ``coverage``: ``pythonpath`` includes
``tests``, so ``tests/coverage/__init__.py`` registered as top-level
``coverage`` and shadowed the installed ``coverage`` distribution. That broke
``pytest-cov`` outright (``ModuleNotFoundError: No module named
'coverage.data'``), so the documented release gate
``uv run pytest --cov=novafabric`` could not run at all. See
``tests/docs/test_test_layout.py``.
"""
