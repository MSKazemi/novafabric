"""End-to-end capture test on SLURM (extends live_slurm_smoke.py).

Submits a Python workload via SlurmRunner that does ONE httpx POST to
a fake LLM endpoint. Checks whether the compute node's wire-level
capture hook fires by inspecting model-calls.jsonl on the shared FS
after the job completes.

Pass criteria: model-calls.jsonl contains at least one record with
``gen_ai.system: openai`` (because the URL matches the registry's
openai pattern).

Fail criteria: empty model-calls.jsonl → SlurmRunner is not injecting
the sitecustomize loader on the compute node, so hooks never install.
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.runners import SlurmRunner

# The workload we'll submit. Makes one httpx POST to a registry-known
# URL with no real network reachability — the connection will fail,
# but the wire-level hook fires before that and records the attempt.
WORKLOAD = textwrap.dedent("""\
    import httpx
    try:
        httpx.post(
            "https://api.openai.com/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role":"user","content":"hi"}]},
            timeout=httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0),
        )
    except Exception as e:
        print(f"(expected) connection failed: {type(e).__name__}", flush=True)
    print("workload done", flush=True)
""")


def main() -> int:
    partition = sys.argv[1] if len(sys.argv) > 1 else "debug"
    base = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        Path.home() / "nova-slurm-e2e"
    )
    base.mkdir(parents=True, exist_ok=True)

    workload_path = base / "workload.py"
    workload_path.write_text(WORKLOAD)

    runs_dir = base / "runs"
    print("== End-to-end capture test on SLURM ==")
    print(f"  partition  : {partition}")
    print(f"  base dir   : {base}")
    print(f"  workload   : {workload_path}")
    print(f"  runs dir   : {runs_dir}")
    print(f"  python     : {sys.executable}")
    print()

    orch = CaptureOrchestrator(base_dir=runs_dir, runner=SlurmRunner())
    result = orch.run(
        command=[sys.executable, str(workload_path)],
        runner_options={"partition": partition, "time": "00:02:00"},
    )

    print("== orchestrator returned ==")
    print(f"  exit_code   : {result.exit_code}")
    print(f"  capsule_dir : {result.capsule_dir}")
    print(f"  run_id      : {result.run_id}")
    print()

    # Inspect the capsule. The defining check: does model-calls.jsonl
    # have an entry from the wire-level hook?
    mc_path = result.capsule_dir / "model-calls.jsonl"
    print(f"== model-calls.jsonl @ {mc_path} ==")
    if not mc_path.exists():
        print("  MISSING — capsule was never finalized")
        return 2
    text = mc_path.read_text().strip()
    if not text:
        print("  EMPTY")
        print()
        print("FAIL: no model calls captured. The wire-level hook did NOT fire on")
        print("      the compute node. Likely cause: SlurmRunner does not inject the")
        print("      sitecustomize loader (PYTHONPATH) the way LocalRunner does.")
        print()
        print("  stdout from the workload:")
        stdout_path = result.capsule_dir / "outputs" / "stdout.txt"
        print((stdout_path.read_text() if stdout_path.exists() else "(no stdout)"))
        return 3
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    print(f"  {len(records)} record(s)")
    for rec in records:
        print(f"    - gen_ai.system={rec.get('gen_ai.system')} "
              f"model={rec.get('gen_ai.request.model')} "
              f"status={rec.get('status')}")
    print()
    if any(r.get("gen_ai.system") == "openai" for r in records):
        print("PASS: wire-level capture fired on the compute node")
        return 0
    print("FAIL: records present but none from the openai URL we hit")
    return 4


if __name__ == "__main__":
    raise SystemExit(main())
