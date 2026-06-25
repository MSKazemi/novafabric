# Example Capsules

This directory contains committed example run capsules showing what NovaFabric produces.

## minimal-run/

A capsule produced by running `examples/minimal-agent-run/agent.py` under `nova capture`.

```bash
nova capture python examples/minimal-agent-run/agent.py
```

| File | Contents |
|---|---|
| `capsule.yaml` | Run manifest: id, status, command, timing, artifact refs |
| `trace.jsonl` | Root execution span |
| `model-calls.jsonl` | LLM API calls (empty — no AI calls in this example) |
| `tool-calls.jsonl` | Tool invocations (empty) |
| `env.lock` | Environment snapshot: Python, packages, OS, hardware |
| `redaction-proof.json` | Proof that no secrets appear in any artifact |
| `replay.yaml` | Replay policy |
| `outputs/stdout.txt` | Agent stdout |
| `inputs/`, `outputs/` | Input and output artifact directories |

To validate this capsule:

```bash
nova validate examples/capsules/minimal-run/
```
