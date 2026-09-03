#!/usr/bin/env python3
"""Generate the animated terminal demo (``docs/assets/demo.svg``).

The README's first screen is the highest-leverage real estate the project has,
and a static code block asks a reader to imagine the tool working. This renders
the real capture → validate → replay → diff sequence as a self-contained
animated SVG: no JavaScript, no external requests, no recording tool to install,
and it animates inside a GitHub README.

**Every line of output here is copied from a real run**, not invented. Terminal
output written from memory has been wrong in this repository before. Re-record
by running the commands and updating ``FRAMES`` when the CLI's output changes.

Usage::

    uv run python scripts/gen_demo_svg.py
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "assets" / "demo.svg"

# Brand tokens from web/src/styles/tokens.css.
BACKGROUND = "#0a0a0c"
FOREGROUND = "#d8d8dd"
ACCENT = "#c4f0a8"
DIM = "#8b8b94"
PROMPT = "#7aa2f7"
CHROME = "#26262c"

CHAR_W = 8.4
LINE_H = 22
PAD_X = 22
PAD_Y = 44
COLS = 78
HOLD = 2.2  # seconds the finished frame stays before looping


@dataclass(frozen=True)
class Line:
    text: str
    kind: str = "out"  # "cmd" | "out" | "ok" | "dim"
    pause: float = 0.35  # seconds before the NEXT line appears


# Real output. Captured 2026-08-05 from novafabric 0.100.1; ULIDs shortened for
# width only.
FRAMES: list[Line] = [
    Line("pip install novafabric", "cmd", 0.6),
    Line("Successfully installed novafabric-0.100.1", "dim", 0.9),
    Line("", "out", 0.2),
    Line("nova capture python agent.py", "cmd", 1.0),
    Line("planning: summarise the Q3 incident report", "dim", 0.25),
    Line("tool: search(index='incidents', q='Q3 outage')  -> 3 hits", "dim", 0.25),
    Line("done: 412 tokens", "dim", 0.4),
    Line("✓ Capsule written: ~/.novafabric/capsules/01KZ9VZPFQB95A63", "ok", 0.9),
    Line("", "out", 0.2),
    Line("nova validate 01KZ9VZPFQB95A63", "cmd", 0.9),
    Line("✓ Valid capsule: 01KZ9VZPFQB95A63  status=success", "ok", 0.9),
    Line("", "out", 0.2),
    Line("nova replay 01KZ9VZPFQB95A63 --mode forensic", "cmd", 1.0),
    Line("✓ Replay written: .novafabric/replays/01KZ9VZX0A9KAXGD", "ok", 0.35),
    Line("  (mode=forensic — no network, no API keys, no tokens spent)", "dim", 0.9),
    Line("", "out", 0.2),
    Line("nova diff 01KZ9VZPFQB95A63 01KZ9VZX0A9KAXGD", "cmd", 1.0),
    Line("Diff: 01KZ9VZPFQB95A63 → 01KZ9VZX0A9KAXGD", "out", 0.25),
    Line("  changed=2  added=0  removed=0", "out", 0.35),
    Line("Outputs:", "out", 0.2),
    Line("  ~ outputs/stdout.txt", "dim", 0.15),
    Line("  ~ outputs/stderr.txt", "dim", 0.9),
]

COLOURS = {"cmd": FOREGROUND, "out": FOREGROUND, "ok": ACCENT, "dim": DIM}


def build() -> str:
    width = int(COLS * CHAR_W + PAD_X * 2)
    height = int(len(FRAMES) * LINE_H + PAD_Y + 24)

    starts: list[float] = []
    clock = 0.4
    for line in FRAMES:
        starts.append(clock)
        clock += line.pause
    total = clock + HOLD

    rows: list[str] = []
    css: list[str] = []

    for index, (line, start) in enumerate(zip(FRAMES, starts)):
        if not line.text:
            continue
        y = PAD_Y + index * LINE_H
        colour = COLOURS[line.kind]
        prefix = (
            f'<tspan fill="{PROMPT}">$</tspan> ' if line.kind == "cmd" else ""
        )
        weight = ' font-weight="600"' if line.kind == "cmd" else ""
        rows.append(
            f'<text class="l{index}" x="{PAD_X}" y="{y}" fill="{colour}"{weight}>'
            f"{prefix}{html.escape(line.text)}</text>"
        )
        # Each line is hidden until its cue, then stays. `animation-fill-mode:
        # both` plus an infinite parent duration is what makes it loop cleanly
        # without JavaScript.
        pct = start / total * 100
        css.append(
            f".l{index}{{opacity:0;animation:r{index} {total:.2f}s infinite}}"
            f"@keyframes r{index}{{0%,{pct:.3f}%{{opacity:0}}"
            f"{min(pct + 0.4, 100):.3f}%,100%{{opacity:1}}}}"
        )

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" \
viewBox="0 0 {width} {height}" role="img" aria-label="NovaFabric terminal demo: \
capture, validate, replay and diff an agent run">
<title>NovaFabric — capture, validate, replay, diff</title>
<style>
text{{font-family:ui-monospace,'JetBrains Mono','DejaVu Sans Mono',monospace;font-size:13px;\
white-space:pre;dominant-baseline:middle}}
{chr(10).join(css)}
@media (prefers-reduced-motion:reduce){{text{{opacity:1!important;animation:none!important}}}}
</style>
<rect width="{width}" height="{height}" rx="10" fill="{BACKGROUND}"/>
<rect width="{width}" height="30" rx="10" fill="{CHROME}"/>
<rect y="20" width="{width}" height="10" fill="{CHROME}"/>
<circle cx="18" cy="15" r="5" fill="#ff5f57"/><circle cx="36" cy="15" r="5" fill="#febc2e"/>\
<circle cx="54" cy="15" r="5" fill="#28c840"/>
<text x="{width // 2}" y="15" fill="{DIM}" text-anchor="middle" font-size="11">novafabric</text>
{chr(10).join(rows)}
</svg>
"""


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(build(), encoding="utf-8")
    kb = OUTPUT.stat().st_size / 1024
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({kb:.1f} KB, {len(FRAMES)} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
