#!/usr/bin/env python3
"""Report JSON-Schema enum values that no source code ever produces.

A schema is a promise. When it declares a value the product itself would have to
emit and nothing emits it, the promise is unkept — and because the schema is the
more visible artifact, the gap tends to be discovered by a consumer rather than by
us.

This found a real defect on 2026-09-02: `replay-result.schema.json` declares
`status: "interrupted"`, the engine never emitted it, and a timed-out replay was
recorded as `{"type": "NonZeroExit", "message": "Replayed command exited with
code 124"}` — a command that was killed, not one that exited.

**This is a report, not a gate.** It exits 0 whatever it finds. Roughly a tenth of
all enum values are legitimately unproduced (see the taxonomy below), and turning
that into a failing check would demand classifying every one of them before the
first useful signal — which is how a gate gets suppressed instead of read.

## Three ways a value is legitimately unproduced

1. **Pass-through** — the value comes from a provider or a parsed payload and is
   copied, never written as a literal. `finish_reason: length`, `role: developer`.
   Any schema field mirroring an external API will look unproduced here.
2. **User-environment reported** — the value describes the user's world, not ours.
   `container.runtime: podman`, `singularity`.
3. **Documented future design** — declared ahead of implementation *and labelled*.
   `session-replay-result`'s `state_seam_match` is emitted `null` and both
   ADR-0123 and the CLI reference say so. That is the honesty rule working, not a
   defect.

Only the fourth kind matters: a value the product must decide to emit, that it
never emits, and that nothing documents as pending.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"
SRC = ROOT / "src" / "novafabric"

#: Verified by hand and found benign. Each entry records *why*, because an
#: unexplained suppression is indistinguishable from an unnoticed defect.
ACKNOWLEDGED: dict[str, str] = {
    "secret-redaction.schema.json": (
        "ScanTarget kinds are labels for capsule surfaces; the scanner reads whole "
        "files, so tool-calls.jsonl covers results too, and env.lock is protected "
        "at write time by a deny-list plus a narrow allowlist rather than by "
        "scanning (verified 2026-09-02)"
    ),
    "session-replay-result.schema.json": (
        "divergence kinds beyond precondition_refusal/replay_failed belong to "
        "D2/P2 state-seam hand-off, documented as future design in ADR-0123 and "
        "docs/cli-reference.md, with the fields emitted null (verified 2026-09-02)"
    ),
    "model-call.schema.json": (
        "provider pass-through — finish_reason, role and operation names are "
        "copied from the provider response, never written as literals"
    ),
    "environment.schema.json": (
        "describes the user's environment (container runtimes, GPU passthrough), "
        "not values NovaFabric decides to emit"
    ),
}

MIN_LEN = 4


def _enums(node: Any, path: str = "") -> Any:
    if isinstance(node, dict):
        if isinstance(node.get("enum"), list):
            yield path, node["enum"]
        for key, value in node.items():
            yield from _enums(value, f"{path}.{key}" if path else key)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _enums(value, f"{path}[{index}]")


def main() -> int:
    blob = "\n".join(
        p.read_text(encoding="utf-8", errors="replace") for p in SRC.rglob("*.py")
    )

    rows: list[tuple[str, str, list[str]]] = []
    total = unproduced = 0
    for schema_path in sorted(SCHEMAS.rglob("*.json")):
        try:
            doc = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = str(schema_path.relative_to(SCHEMAS))
        for path, values in _enums(doc):
            candidates = [v for v in values if isinstance(v, str) and len(v) >= MIN_LEN]
            if not candidates:
                continue
            absent = [
                v for v in candidates if f'"{v}"' not in blob and f"'{v}'" not in blob
            ]
            total += len(candidates)
            unproduced += len(absent)
            # A wholly-unproduced enum is usually a type the product does not own
            # at all; a partially-produced one is where a real gap hides.
            if absent and len(absent) < len(candidates):
                rows.append((name, path, absent))

    unacknowledged = [r for r in rows if r[0] not in ACKNOWLEDGED]

    print(f"{unproduced} of {total} enum values (len>={MIN_LEN}) never appear "
          f"as a literal in src/novafabric\n")
    print(f"{len(rows)} partially-produced enum(s); "
          f"{len(rows) - len(unacknowledged)} acknowledged, "
          f"{len(unacknowledged)} to review\n")

    for name, path, absent in unacknowledged:
        print(f"  {name}")
        print(f"    {path}")
        print(f"    unproduced: {absent}")

    if unacknowledged:
        print(
            "\nFor each: is it pass-through, user-environment, or documented "
            "future design? If none of those, the product declares a value it "
            "never emits — read the producer before concluding either way."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
