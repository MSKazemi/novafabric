"""Test package marker — see tests/capsule/__init__.py.

Without this, pytest imports test modules by bare basename, so two files
named the same in different directories collide at collection.
"""
