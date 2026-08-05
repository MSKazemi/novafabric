"""Test package marker — see tests/capsule/__init__.py.

Without this, pytest imports test modules by bare basename, so two files
named the same in different directories collide at collection. That is not
hypothetical: tests/embodied/ and tests/federation/ both shipped a
test_facet.py and broke collection outright.
"""
