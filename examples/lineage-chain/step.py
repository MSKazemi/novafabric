"""Lineage example: a parameterized 'agent step' that declares which
asset it consumed. Three captures of this script with different step
names (and a shared consumed asset) build up a small lineage graph
that demonstrates `nova lineage blast-radius` and `provenance`.

The script appends one record to the capsule's `assets.jsonl`. The
capture orchestrator's lineage writer turns that into a `consumed`
edge in the local lineage graph (see src/novafabric/lineage/_writer.py).

Run from the README — not directly.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: step.py <step-name> [consumed-asset-ref]", file=sys.stderr)
        return 2

    step_name = sys.argv[1]
    asset_ref = sys.argv[2] if len(sys.argv) >= 3 else "datasets/training-set@1.0.0"

    capsule_dir = os.environ.get("NOVAFABRIC_CAPSULE_DIR")
    if capsule_dir:
        # Declare the asset this step consumed. The capture orchestrator
        # reads assets.jsonl when it writes lineage.jsonl.
        record = {"asset_ref": asset_ref, "registry": "local"}
        with (Path(capsule_dir) / "assets.jsonl").open("a") as f:
            f.write(json.dumps(record) + "\n")
        print(f"[{step_name}] declared consumed asset: local:{asset_ref}")
    else:
        print(f"[{step_name}] not running under nova capture — no lineage edge written")

    print(f"[{step_name}] step finished ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
