"""MCP conformance surfaces (NF-038/039) and the read-only evidence server.

SEP-1649 Server Card publishing and validation. The MCP capture and proxy
paths live in ``capture/hooks/_mcp.py`` and ``proxy/mcp_proxy.py``.

``evidence_server`` / ``evidence_tools`` are the experimental read-only stdio
MCP evidence server (issue #244), run with
``python -m novafabric.mcp.evidence_server --scope DIR``.
"""
