"""Hatchling metadata hook: make the PyPI long description self-contained.

README.md is written for GitHub, where ``docs/foo.md`` and ``docs/assets/demo.svg``
resolve against the repository. On pypi.org the same text is rendered with no
repository root, so every relative link and image breaks. This hook rewrites them
to absolute URLs pinned to the release tag (``v{version}``) at build time; the
README file itself is never modified, so GitHub rendering is unchanged.

Pure stdlib, deterministic, no network. ``rewrite_readme`` is importable so tests
can run it directly on the real README (see tests/packaging_metadata/).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from hatchling.metadata.plugin.interface import MetadataHookInterface
except ImportError:  # pragma: no cover - tests import the pure functions without hatchling
    MetadataHookInterface = object  # type: ignore[assignment,misc]

REPO = "MSKazemi/novafabric"
_MEDIA_EXTS = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".mp4", ".webm", ".mov"}
)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

# Markdown inline destination: `](dest` or `](<dest>` — optionally followed by a title.
_MD_INLINE = re.compile(r"(\]\()(<[^>\n]*>|[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)")
# Markdown reference definition: `[label]: dest`
_MD_REFDEF = re.compile(r"^(\s{0,3}\[[^\]\n]+\]:\s*)(<[^>\n]*>|\S+)", re.MULTILINE)
# HTML attributes
_HTML_ATTR = re.compile(r"""(\b(?:src|href)\s*=\s*)(["'])([^"']*)\2""", re.IGNORECASE)
_FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,}).*?^\1\2[`~]*[ \t]*$", re.MULTILINE | re.DOTALL)


def is_relative(target: str) -> bool:
    """True when *target* resolves against the repository (needs rewriting)."""
    t = target.strip()
    return bool(t) and not t.startswith(("#", "//")) and not _SCHEME.match(t)


def absolutize(target: str, version: str) -> str:
    """Return the absolute URL for a relative repo path; others are unchanged."""
    if not is_relative(target):
        return target
    t = target.strip()
    path, sep, frag = t.partition("#")
    path, qsep, query = path.partition("?")
    path = path.lstrip("/")
    while path.startswith("./"):
        path = path[2:]
    leaf = path.rsplit("/", 1)[-1]
    ext = "." + leaf.rsplit(".", 1)[-1].lower() if "." in leaf else ""
    if ext in _MEDIA_EXTS:
        base = f"https://raw.githubusercontent.com/{REPO}/v{version}/"
    elif path.endswith("/"):
        base = f"https://github.com/{REPO}/tree/v{version}/"
    else:
        base = f"https://github.com/{REPO}/blob/v{version}/"
    return base + path + (qsep + query) + (sep + frag)


def rewrite_readme(text: str, version: str) -> str:
    """Rewrite every relative link/image/HTML src/href in *text*; skip code fences."""

    def fix_dest(dest: str) -> str:
        if dest.startswith("<") and dest.endswith(">"):
            return "<" + absolutize(dest[1:-1], version) + ">"
        return absolutize(dest, version)

    def rewrite_chunk(chunk: str) -> str:
        chunk = _MD_INLINE.sub(lambda m: m.group(1) + fix_dest(m.group(2)), chunk)
        chunk = _MD_REFDEF.sub(lambda m: m.group(1) + fix_dest(m.group(2)), chunk)
        return _HTML_ATTR.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{absolutize(m.group(3), version)}{m.group(2)}",
            chunk,
        )

    out: list[str] = []
    last = 0
    for fence in _FENCE.finditer(text):
        out.append(rewrite_chunk(text[last : fence.start()]))
        out.append(fence.group(0))
        last = fence.end()
    out.append(rewrite_chunk(text[last:]))
    return "".join(out)


class PyPIReadmeHook(MetadataHookInterface):
    PLUGIN_NAME = "custom"

    def update(self, metadata: dict[str, Any]) -> None:
        readme = Path(self.root) / "README.md"
        metadata["readme"] = {
            "content-type": "text/markdown",
            "text": rewrite_readme(readme.read_text(encoding="utf-8"), metadata["version"]),
        }
