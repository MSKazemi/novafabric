#!/usr/bin/env python3
"""Generate the animated capsule-lifecycle diagram (``docs/assets/lifecycle.svg``).

The README explains the product in prose; this shows it in nine seconds. A
command is captured into a Run Capsule, the capsule's files appear, the seal is
added, and the capsule feeds the four things you do with it: replay, diff,
lineage, and verify-and-export.

Self-contained like ``gen_demo_svg.py``: no JavaScript, no external fonts or
requests, so it animates inside a GitHub README and on PyPI. Movement uses SMIL
``animateMotion`` (supported by every current browser, including inside
``<img>``); fades use CSS keyframes, which ``prefers-reduced-motion`` turns off.

Every label states something the code does today — keep it that way when you
edit ``FILES`` or ``OUTCOMES``.

Usage::

    uv run python scripts/gen_lifecycle_svg.py
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "assets" / "lifecycle.svg"

# Brand tokens from docs/press-kit.md (palette) and ui/dashboard/src/styles/tokens.css.
BACKGROUND = "#0a0a0c"
PANEL = "#111114"
BORDER = "#26262c"
TEXT = "#e6e6ea"
MUTED = "#a8a8b3"
LIME = "#c4f0a8"
TEAL = "#63cad0"

WIDTH = 960
HEIGHT = 360
LOOP = 9.0  # seconds

SANS = "-apple-system,'Segoe UI','DM Sans','Helvetica Neue',Arial,sans-serif"
MONO = "ui-monospace,'JetBrains Mono','DejaVu Sans Mono',Menlo,monospace"


@dataclass(frozen=True)
class Outcome:
    title: str
    detail: str
    command: str


# Files every capture writes (the seal only when NovaSeal is configured).
FILES: list[str] = [
    "capsule.yaml",
    "model-calls.jsonl",
    "tool-calls.jsonl",
    "env.lock",
    "lineage.jsonl",
    "redaction-proof.json",
]

OUTCOMES: list[Outcome] = [
    Outcome("Replay", "inspect or re-run it", "nova replay"),
    Outcome("Diff", "what changed between runs", "nova diff"),
    Outcome("Lineage", "what it depended on", "nova lineage"),
    Outcome("Verify & export", "signed Evidence Bundle", "nova verify"),
]


def _fade_in(name: str, start: float, end_hold: float = LOOP - 0.6) -> str:
    """CSS keyframes: hidden until ``start``, visible until ``end_hold``, then reset."""
    a = start / LOOP * 100
    b = min(a + 3, 100)
    c = end_hold / LOOP * 100
    d = min(c + 4, 100)
    return (
        f".{name}{{opacity:0;animation:{name} {LOOP}s infinite}}"
        f"@keyframes {name}{{0%,{a:.2f}%{{opacity:0}}{b:.2f}%,{c:.2f}%{{opacity:1}}"
        f"{d:.2f}%,100%{{opacity:0}}}}"
    )


def _glow(name: str, start: float) -> str:
    """CSS keyframes: a node's border turns lime when its pulse arrives."""
    a = start / LOOP * 100
    b = min(a + 3, 100)
    c = (LOOP - 0.6) / LOOP * 100
    return (
        f".{name}{{animation:{name} {LOOP}s infinite}}"
        f"@keyframes {name}{{0%,{a:.2f}%{{stroke:{BORDER}}}{b:.2f}%,{c:.2f}%{{stroke:{LIME}}}"
        f"100%{{stroke:{BORDER}}}}}"
    )


def _pulse(path_id: str, start: float, travel: float, colour: str = LIME) -> str:
    """A dot that travels along ``path_id`` once per loop (SMIL)."""
    t0 = start / LOOP
    t1 = min((start + travel) / LOOP, 1.0)
    t2 = min(t1 + 0.001, 1.0)
    return (
        f'<circle class="pulse" r="4.5" fill="{colour}" opacity="0">'
        f'<animate attributeName="opacity" dur="{LOOP}s" repeatCount="indefinite" '
        f'keyTimes="0;{t0:.4f};{t0 + 0.001:.4f};{t1:.4f};{t2:.4f};1" values="0;0;1;1;0;0"/>'
        f'<animateMotion dur="{LOOP}s" repeatCount="indefinite" calcMode="linear" '
        f'keyPoints="0;0;1;1" keyTimes="0;{t0:.4f};{t1:.4f};1">'
        f'<mpath href="#{path_id}"/></animateMotion></circle>'
    )


def build() -> str:
    css: list[str] = []
    body: list[str] = []
    paths: list[str] = []

    # --- Source: the command being captured -------------------------------------------
    src_x, src_y, src_w, src_h = 28, 132, 196, 96
    body.append(
        f'<rect x="{src_x}" y="{src_y}" width="{src_w}" height="{src_h}" rx="10" '
        f'fill="{PANEL}" stroke="{BORDER}" stroke-width="1.5"/>'
        f'<text x="{src_x + 16}" y="{src_y + 30}" class="t">Your agent</text>'
        f'<text x="{src_x + 16}" y="{src_y + 52}" class="m">any command,</text>'
        f'<text x="{src_x + 16}" y="{src_y + 70}" class="m">no code changes</text>'
    )

    # --- Capture arrow --------------------------------------------------------------------
    cap_x, cap_y, cap_w, cap_h = 330, 46, 250, 268
    paths.append(f'<path id="p-capture" d="M {src_x + src_w} 180 L {cap_x} 180"/>')
    body.append(
        f'<path d="M {src_x + src_w} 180 L {cap_x - 6} 180" stroke="{BORDER}" '
        f'stroke-width="2" fill="none" marker-end="url(#arrow)"/>'
        f'<text x="{(src_x + src_w + cap_x) // 2}" y="168" class="c" text-anchor="middle">'
        f"nova capture</text>"
    )

    # --- The Run Capsule ------------------------------------------------------------------
    body.append(
        f'<rect class="g-cap" x="{cap_x}" y="{cap_y}" width="{cap_w}" height="{cap_h}" rx="12" '
        f'fill="{PANEL}" stroke="{BORDER}" stroke-width="1.5"/>'
        f'<text x="{cap_x + 18}" y="{cap_y + 32}" class="t" style="fill:{LIME}">Run Capsule</text>'
        f'<text x="{cap_x + 18}" y="{cap_y + 52}" class="m">a folder you own</text>'
    )
    css.append(_glow("g-cap", 1.4))
    file_start = 1.6
    for i, name in enumerate(FILES):
        cls = f"f{i}"
        y = cap_y + 84 + i * 26
        body.append(
            f'<text class="{cls} c" x="{cap_x + 22}" y="{y}">'
            f'<tspan fill="{MUTED}">▸ </tspan>{html.escape(name)}</text>'
        )
        css.append(_fade_in(cls, file_start + i * 0.32))
    seal_t = file_start + len(FILES) * 0.32 + 0.3
    seal_y = cap_y + 84 + len(FILES) * 26
    body.append(
        f'<g class="seal"><text x="{cap_x + 22}" y="{seal_y}" class="c" style="fill:{TEAL}">'
        f"▸ .seal/  signed · logged</text></g>"
    )
    css.append(_fade_in("seal", seal_t))
    body.append(
        f'<text x="{cap_x + cap_w - 18}" y="{cap_y + 32}" class="m" text-anchor="end">'
        f"secret-scanned</text>"
    )

    # --- Outcomes -------------------------------------------------------------------------
    out_x, out_w, out_h, gap = 690, 244, 64, 14
    total_h = len(OUTCOMES) * out_h + (len(OUTCOMES) - 1) * gap
    top = (HEIGHT - total_h) // 2
    fan_start = seal_t + 0.6
    origin_x, origin_y = cap_x + cap_w, cap_y + cap_h // 2
    for i, out in enumerate(OUTCOMES):
        y = top + i * (out_h + gap)
        cy = y + out_h // 2
        pid = f"p-out{i}"
        mid = (origin_x + out_x) // 2
        d = f"M {origin_x} {origin_y} C {mid} {origin_y} {mid} {cy} {out_x} {cy}"
        paths.append(f'<path id="{pid}" d="{d}"/>')
        body.append(
            f'<path d="M {origin_x} {origin_y} C {mid} {origin_y} {mid} {cy} {out_x - 6} {cy}" '
            f'stroke="{BORDER}" stroke-width="2" fill="none" marker-end="url(#arrow)"/>'
        )
        arrive = fan_start + i * 0.35 + 0.9
        gcls = f"g-out{i}"
        body.append(
            f'<rect class="{gcls}" x="{out_x}" y="{y}" width="{out_w}" height="{out_h}" rx="10" '
            f'fill="{PANEL}" stroke="{BORDER}" stroke-width="1.5"/>'
            f'<text x="{out_x + 16}" y="{y + 26}" class="t">{html.escape(out.title)}</text>'
            f'<text x="{out_x + out_w - 14}" y="{y + 26}" class="c" text-anchor="end" '
            f'style="fill:{MUTED}">{html.escape(out.command)}</text>'
            f'<text x="{out_x + 16}" y="{y + 47}" class="m">{html.escape(out.detail)}</text>'
        )
        css.append(_glow(gcls, arrive))
        body.append(_pulse(pid, fan_start + i * 0.35, 0.9, TEAL if i == 3 else LIME))

    body.insert(0, _pulse("p-capture", 0.4, 1.0))

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" \
viewBox="0 0 {WIDTH} {HEIGHT}" role="img" aria-label="NovaFabric lifecycle: a command is \
captured into a secret-scanned Run Capsule, optionally sealed, and the capsule is then replayed, \
diffed, traced for lineage, and verified and exported as a signed Evidence Bundle">
<title>NovaFabric — capture once, then replay, diff, trace and verify</title>
<defs>
<marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" \
orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{BORDER}"/></marker>
{chr(10).join(paths).replace("<path ", '<path fill="none" ')}
</defs>
<style>
.t{{font-family:{SANS};font-size:16px;font-weight:600;fill:{TEXT}}}
.m{{font-family:{SANS};font-size:13px;fill:{MUTED}}}
.c{{font-family:{MONO};font-size:12.5px;fill:{TEXT};white-space:pre}}
{chr(10).join(css)}
@media (prefers-reduced-motion:reduce){{*{{animation:none!important;opacity:1!important}}\
.pulse{{display:none}}}}
</style>
<rect width="{WIDTH}" height="{HEIGHT}" rx="14" fill="{BACKGROUND}"/>
{chr(10).join(body)}
</svg>
"""


def main() -> None:
    svg = build()
    OUTPUT.write_text(svg, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({len(svg) / 1024:.1f} KB)")


if __name__ == "__main__":
    main()
