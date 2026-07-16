"""Generate two synthetic scores.jsonl files for the `nova diff --significance` demo.

Stdlib only. Simulates the stored outcome history of a boolean `task_pass`
metric over 50 baseline runs (47 pass) and 50 candidate runs (38 pass) —
the shape `nova eval offline --emit-score` / `nova eval score add` would
accumulate across real runs. `nova diff --significance` is pure arithmetic
over these recorded outcomes; nothing is re-run.

Usage: python make_scores.py <out-dir>
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

CARD_DIGEST = "sha256:" + hashlib.sha256(b"demo-eval-card").hexdigest()


def write_scores(path: Path, n: int, fail_every: int) -> None:
    """Write *n* boolean scores, failing every *fail_every*-th run (interleaved).

    The SPRT in `nova diff --significance` is sequential — it walks the outcome
    sequence in order — so failures are spread through the history the way real
    flaky-run records would be, not batched at the end.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for i in range(n):
            subject = "sha256:" + hashlib.sha256(f"demo-run-{i}".encode()).hexdigest()
            record = {
                "subject": subject,
                "subject_kind": "capsule",
                "name": "task_pass",
                "value": (i + 1) % fail_every != 0,
                "value_type": "boolean",
                "source": "code",
                "evaluator_id": "demo-eval",
                "eval_card_digest": CARD_DIGEST,
            }
            f.write(json.dumps(record) + "\n")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    write_scores(out / "baseline" / "scores.jsonl", n=50, fail_every=16)  # 47/50 pass
    write_scores(out / "candidate" / "scores.jsonl", n=50, fail_every=4)  # 38/50 pass
    print(f"wrote {out}/baseline/scores.jsonl (47/50 pass)")
    print(f"wrote {out}/candidate/scores.jsonl (38/50 pass)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
