#!/usr/bin/env python3
"""Generate 1000 reference EventEnvelope corpus events (deterministic seed).

Usage:
    cd schemas/event-envelope-v1/corpus
    python3 generate_corpus.py

Output: 0001.json through 1000.json in the current directory.
Seed: 42 — re-running produces byte-identical output.
"""

import json
import pathlib
import random
import hashlib
import datetime
import base64
import struct

random.seed(42)

SCRIPT_DIR = pathlib.Path(__file__).parent

EVENT_TYPES = [
    "run.start",
    "run.end",
    "model_call",
    "tool_call",
    "span",
    "capsule.finalize",
]

AGENT_IDS = [
    "nova-sdk/python",
    "nova-otel-bridge",
    "langchain-agent",
    "openai-agents-sdk",
    "anthropic-sdk",
    "custom-agent-v1",
]

CLUSTER_IDS = [
    "hpc-cluster-01",
    "k8s-prod-us-east-1",
    "slurm-train-v100",
    "local-dev",
    "azure-ml-cluster",
]

TENANT_IDS = [
    "tenant-acme",
    "tenant-research-lab",
    "tenant-platform-eng",
    "tenant-ml-ops",
]

NODE_IDS = [
    "node01.cluster.local",
    "node02.cluster.local",
    "worker-0001",
    "gpu-node-04",
    "head-node",
]

AI_MODELS = [
    "gpt-4o",
    "gpt-4-turbo",
    "claude-3-5-sonnet-20241022",
    "claude-3-opus-20240229",
    "meta-llama/Llama-3.1-70B-Instruct",
    "mistralai/Mistral-7B-Instruct-v0.3",
]

AI_SYSTEMS = ["openai", "anthropic", "meta", "mistral"]

TOOL_NAMES = [
    "bash_executor",
    "file_reader",
    "web_search",
    "code_interpreter",
    "database_query",
    "http_client",
]

# Crockford Base32 alphabet for ULID
CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """Generate a random ULID-like string (not timestamp-accurate, just format-valid)."""
    # First char must be 0-7 (3 bits for timestamp high)
    first = random.choice("0123456")
    rest = "".join(random.choices(CROCKFORD_ALPHABET, k=25))
    return first + rest


def uuid_v7() -> str:
    """Generate a random UUID v7-like string (format-valid, not time-accurate)."""
    # 8-4-7xxx-[89ab]xxx-12
    p1 = "".join(random.choices("0123456789abcdef", k=8))
    p2 = "".join(random.choices("0123456789abcdef", k=4))
    p3 = "7" + "".join(random.choices("0123456789abcdef", k=3))
    p4 = random.choice("89ab") + "".join(random.choices("0123456789abcdef", k=3))
    p5 = "".join(random.choices("0123456789abcdef", k=12))
    return f"{p1}-{p2}-{p3}-{p4}-{p5}"


def trace_id() -> str:
    """Generate a valid 32-char lowercase hex trace ID (non-zero)."""
    val = "".join(random.choices("0123456789abcdef", k=32))
    # Ensure not all-zeros
    while val == "0" * 32:
        val = "".join(random.choices("0123456789abcdef", k=32))
    return val


def span_id() -> str:
    """Generate a valid 16-char lowercase hex span ID (non-zero)."""
    val = "".join(random.choices("0123456789abcdef", k=16))
    while val == "0" * 16:
        val = "".join(random.choices("0123456789abcdef", k=16))
    return val


def uuid_v4() -> str:
    """Generate a format-valid UUID v4 string."""
    p1 = "".join(random.choices("0123456789abcdef", k=8))
    p2 = "".join(random.choices("0123456789abcdef", k=4))
    p3 = "4" + "".join(random.choices("0123456789abcdef", k=3))
    p4 = random.choice("89ab") + "".join(random.choices("0123456789abcdef", k=3))
    p5 = "".join(random.choices("0123456789abcdef", k=12))
    return f"{p1}-{p2}-{p3}-{p4}-{p5}"


def iso8601_ts(base_offset_seconds: int = 0) -> str:
    """Generate an ISO 8601 datetime with timezone offset."""
    base = datetime.datetime(2026, 5, 12, 10, 0, 0, tzinfo=datetime.timezone.utc)
    dt = base + datetime.timedelta(seconds=base_offset_seconds)
    # Add millisecond noise
    ms = random.randint(0, 999)
    dt = dt.replace(microsecond=ms * 1000)
    return dt.isoformat()


def payload_hash(payload_bytes: bytes) -> str:
    """Compute sha256:<hex> hash of payload bytes."""
    digest = hashlib.sha256(payload_bytes).hexdigest()
    return f"sha256:{digest}"


def fake_signature() -> str:
    """Generate a fake 88-char base64 Ed25519-like signature."""
    raw = bytes(random.randint(0, 255) for _ in range(64))
    return base64.b64encode(raw).decode()


def model_call_payload(model: str, system: str) -> dict:
    input_tokens = random.randint(50, 4096)
    output_tokens = random.randint(10, 1024)
    return {
        "model": model,
        "system": system,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "stop_reason": random.choice(["stop", "max_tokens", "tool_use"]),
    }


def tool_call_payload(tool_name: str) -> dict:
    return {
        "tool_name": tool_name,
        "tool_call_id": "tc_" + "".join(random.choices("0123456789abcdef", k=16)),
        "arguments": {"input": "example input for " + tool_name},
        "duration_ms": random.randint(5, 5000),
    }


def generate_event(index: int) -> dict:
    """Generate a single EventEnvelope event (1-based index)."""
    i = index  # 1-based
    event_type = EVENT_TYPES[(i - 1) % len(EVENT_TYPES)]

    # global_run_id: alternate between ULID and UUID v7
    if i % 3 == 0:
        g_run_id = uuid_v7()
    else:
        g_run_id = ulid()

    run_id_val = ulid()

    # parent_run_id: null for every 5th event
    if i % 5 == 0:
        parent_run_id_val = None
    else:
        parent_run_id_val = ulid()

    # cluster_id: null for first 200 events
    if i <= 200:
        cluster_id_val = None
    else:
        cluster_id_val = random.choice(CLUSTER_IDS)

    # tenant_id: null for first 100 events
    if i <= 100:
        tenant_id_val = None
    else:
        tenant_id_val = random.choice(TENANT_IDS)

    # emitter_node_id: present for events 401+
    if i >= 401:
        emitter_node_id_val = random.choice(NODE_IDS)
    else:
        emitter_node_id_val = None

    agent_id_val = random.choice(AGENT_IDS)

    event = {
        "envelope_version": "1",
        "schema_version": "event-envelope/1",
        "event_id": ulid(),
        "global_run_id": g_run_id,
        "run_id": run_id_val,
        "parent_run_id": parent_run_id_val,
        "event_type": event_type,
        "trace_id": trace_id(),
        "span_id": span_id(),
        "agent_id": agent_id_val,
        "cluster_id": cluster_id_val,
        "tenant_id": tenant_id_val,
        "started_at": iso8601_ts(base_offset_seconds=i * 60),
    }

    if emitter_node_id_val is not None:
        event["emitter_node_id"] = emitter_node_id_val

    # payload and payload_hash for model_call and tool_call
    if event_type == "model_call":
        model = random.choice(AI_MODELS)
        system = random.choice(AI_SYSTEMS)
        payload = model_call_payload(model, system)
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        event["payload"] = payload
        event["payload_hash"] = payload_hash(payload_bytes)
    elif event_type == "tool_call":
        tool_name = random.choice(TOOL_NAMES)
        payload = tool_call_payload(tool_name)
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        event["payload"] = payload
        event["payload_hash"] = payload_hash(payload_bytes)
    else:
        event["payload"] = None
        event["payload_hash"] = None

    # NovaSeal signature: present for events 501+
    if i >= 501:
        event["nova.batch.signature"] = fake_signature()
        event["nova.batch.signing_key_id"] = uuid_v4()
    else:
        event["nova.batch.signature"] = None
        event["nova.batch.signing_key_id"] = None

    return event


def main() -> None:
    out_dir = SCRIPT_DIR
    generated = 0
    for i in range(1, 1001):
        event = generate_event(i)
        filename = out_dir / f"{i:04d}.json"
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(event, f, indent=2, ensure_ascii=False)
            f.write("\n")
        generated += 1

    print(f"Generated {generated} corpus events in {out_dir}/")
    print(f"Files: {out_dir}/0001.json ... {out_dir}/1000.json")


if __name__ == "__main__":
    main()
