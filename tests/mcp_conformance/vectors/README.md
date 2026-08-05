# MCP conformance vectors (NF-038 R9)

Recorded request/response pairs that pin wire compatibility with MCP
**2026-07-28**. `nova mcp conformance <dir>` replays each and asserts the
capture shape this spec requires.

These exist so a **spec drift fails a PR** rather than surfacing as silently
wrong evidence months later. Each vector is a JSON object:

```json
{
  "name": "...",                 // human label, shown on failure
  "why": "...",                  // what would break if this regressed
  "protocol_version": "...",     // negotiated version, or null
  "messages": [                  // ordered legs
    {"direction": "server_to_client", "message": { ...JSON-RPC... }}
  ],
  "expect": {
    "record_count": 2,
    "rounds": [1, 1],
    "directions": ["server_to_client", "client_to_server"],
    "shared_exchange_id": true,
    "no_raw_values": ["a-secret-that-must-not-appear"]
  }
}
```

Adding a vector is the cheapest way to lock in a wire behaviour you have just
debugged — prefer it to a comment.
