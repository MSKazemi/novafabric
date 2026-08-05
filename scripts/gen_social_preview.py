#!/usr/bin/env python3
"""Generate the GitHub social preview card (``docs/assets/social-preview.png``).

Without a custom social preview, every share of the repository — X, LinkedIn,
Slack, Discord, Hacker News — renders GitHub's default grey card. That card is
the first impression for most people who ever see the project, and it says
nothing.

GitHub's REST API cannot set this image; it is uploaded once through
*Settings → General → Social preview*. This script produces the file so the
upload is the only manual step, and so the card can be regenerated
deterministically when the tagline changes.

Spec: 1280×640 (GitHub renders it at 1280×640 and crops to 1200×600 on some
surfaces, so nothing important goes within 40 px of an edge).

Usage::

    uv run python scripts/gen_social_preview.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "docs" / "assets" / "social-preview.png"

WIDTH, HEIGHT = 1280, 640
MARGIN = 88

# Brand tokens from web/src/styles/tokens.css — keep these in sync with the
# dashboard so the card and the product look like the same project.
BACKGROUND = "#0a0a0c"
ACCENT = "#c4f0a8"
FOREGROUND = "#fbfbfc"
MUTED = "#8b8b94"
RULE = "#26262c"

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"
FONT_REGULAR = FONT_DIR / "DejaVuSans.ttf"
FONT_MONO = FONT_DIR / "DejaVuSansMono.ttf"

TITLE = "NovaFabric"
TAGLINE = [
    "Capture, replay, diff and audit AI agent",
    "and model runs as portable evidence capsules.",
]
COMMAND = "$ nova capture python my_agent.py"
FOOTER_LEFT = "Open source · Apache-2.0 · Self-hosted"
FOOTER_RIGHT = "github.com/novafabric/novafabric"


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    if not path.exists():  # pragma: no cover - environment dependent
        return ImageFont.load_default(size)
    return ImageFont.truetype(str(path), size)


def build() -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), BACKGROUND)
    draw = ImageDraw.Draw(image)

    # Accent bar down the left edge — the one piece of colour, so the card is
    # recognisable as this project at thumbnail size.
    draw.rectangle([(0, 0), (14, HEIGHT)], fill=ACCENT)

    y = MARGIN

    draw.text((MARGIN, y), TITLE, font=_font(FONT_BOLD, 82), fill=FOREGROUND)
    y += 118

    tagline_font = _font(FONT_REGULAR, 38)
    for line in TAGLINE:
        draw.text((MARGIN, y), line, font=tagline_font, fill=MUTED)
        y += 52

    y += 40
    draw.line([(MARGIN, y), (WIDTH - MARGIN, y)], fill=RULE, width=2)
    y += 44

    draw.text((MARGIN, y), COMMAND, font=_font(FONT_MONO, 32), fill=ACCENT)

    footer_font = _font(FONT_REGULAR, 26)
    footer_y = HEIGHT - MARGIN - 10
    draw.text((MARGIN, footer_y), FOOTER_LEFT, font=footer_font, fill=MUTED)

    right = draw.textlength(FOOTER_RIGHT, font=footer_font)
    draw.text((WIDTH - MARGIN - right, footer_y), FOOTER_RIGHT, font=footer_font, fill=MUTED)

    return image


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    build().save(OUTPUT, "PNG", optimize=True)
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)} ({WIDTH}x{HEIGHT}, {size_kb:.0f} KB)")
    print("Upload it at: Settings -> General -> Social preview")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
