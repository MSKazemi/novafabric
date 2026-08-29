"""A stand-in for "the training script", deliberately tiny.

Standard library only: no torch, no GPU, no cluster, no API key. It runs in
about a second, prints deterministic output, and writes one small artifact, so
the example is about the capture pattern rather than about the workload.

Resisting the urge to make this realistic is the point. A reader should be able
to run the identical payload on their laptop, see the resulting capsule, and only
then care about the batch script.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

STEPS = 20


def _slurm_context() -> dict[str, str]:
    """Whatever Slurm told us, or an honest empty record when run locally.

    These are read, not required. The same payload must run identically outside
    a scheduler, which is what makes the example testable in CI.
    """
    keys = (
        "SLURM_JOB_ID",
        "SLURM_JOB_NAME",
        "SLURM_CLUSTER_NAME",
        "SLURMD_NODENAME",
        "SLURM_JOB_NUM_NODES",
        "SLURM_CPUS_ON_NODE",
    )
    return {k: os.environ[k] for k in keys if k in os.environ}


def main() -> int:
    ctx = _slurm_context()
    print(f"payload: host     = {platform.node()}")
    print(f"payload: python   = {sys.version.split()[0]}")
    print(f"payload: scheduler= {'slurm' if ctx else 'none (running locally)'}")
    for key, value in sorted(ctx.items()):
        print(f"payload: {key} = {value}")

    # A deterministic "training loop": same input, same output, every time.
    loss = 1.0
    for step in range(1, STEPS + 1):
        loss = round(loss * 0.9, 10)
        if step % 5 == 0:
            print(f"payload: step={step:3d} loss={loss:.6f}")

    out = Path(os.environ.get("NOVAFABRIC_EXAMPLE_OUT", "metrics.json"))
    out.write_text(json.dumps({"steps": STEPS, "final_loss": loss}, indent=2) + "\n")
    print(f"payload: wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
