"""Test package marker — see tests/capsule/__init__.py.

Named ``mcp_evidence_server`` rather than ``mcp`` on purpose: with
``pythonpath = tests`` a ``tests/mcp`` package would shadow the installed MCP
SDK that ``test_stdio_transport.py`` drives as a real client.
"""
