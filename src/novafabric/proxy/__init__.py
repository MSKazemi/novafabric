"""Transparent capture proxies (ADR-0015 §Secondary).

The proxy package provides standalone processes that sit between an
uninstrumented agent and an upstream service, recording the wire-level
exchange to an active capsule. Unlike the SDK hooks in ``capture/hooks/``,
proxies do not require the agent to be a Python process or to import any
NovaFabric code.

v0.5.x ships ``mcp_proxy`` (stdio transport, passthrough only).
"""
