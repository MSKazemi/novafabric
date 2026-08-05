#!/usr/bin/env python3
"""Fail when a public markdown file links to something a reader cannot open.

This gate exists because of a real defect: 142 markdown links across the public
docs pointed into the private ``design/`` tree, so every one of them 404'd for
every visitor — including the RFC-process link that ``CONTRIBUTING.md`` tells
contributors to read first.

**The test is "is the target tracked by the public git", not "does the file
exist".** Those differ precisely where it matters. This repository keeps one
working tree and two gits: the public one excludes ``design/``, ``.claude/``,
``CLAUDE.md``, ``monetize/``, ``bench/``, ``site-config/`` and
``THREAT_MODEL.md``. All of those are present on a maintainer's disk, so an
existence check passes locally and fails only for the reader the docs are for.
Checking git membership instead is what caught ``CONTRIBUTING.md`` →
``THREAT_MODEL.md``, an instance a prefix denylist had missed.

The checker walks every git-tracked markdown file and resolves each *relative*
link target. External links (``http``, ``mailto``) and pure anchors are out of
scope — verifying those needs the network and this gate must stay offline and
fast.

Usage::

    uv run python scripts/check_doc_links.py
    uv run python scripts/check_doc_links.py --quiet
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# [label](target) — target captured up to the closing paren. Titles ("...") are
# stripped separately.
_LINK = re.compile(r"\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")

# Fenced code blocks are stripped before scanning: a link inside an example is
# documentation of a link, not a link.
_FENCE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)

_SKIP_SCHEMES = ("http://", "https://", "mailto:", "ftp://", "tel:", "data:", "#")

# Generated or build-time paths that are legitimately absent from git but are
# real for a reader of the built artifact.
_ALLOWED_UNTRACKED = ("src/novafabric/serve/static/",)


@dataclass(frozen=True)
class Broken:
    source: Path
    target: str
    reason: str

    def render(self) -> str:
        rel = self.source.relative_to(REPO_ROOT)
        return f"{rel}: {self.target}  ({self.reason})"


def _git_ls_files(*patterns: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", *patterns],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.splitlines()


def tracked_markdown() -> list[Path]:
    return [REPO_ROOT / rel for rel in _git_ls_files("*.md")]


def tracked_paths() -> frozenset[str]:
    """Every path the public git tracks, plus each of their parent directories.

    Directories are included so that a link to ``docs/rfcs/`` — legitimate, and
    how a reader browses on GitHub — resolves even though git tracks only files.
    """
    paths: set[str] = set()
    for rel in _git_ls_files():
        paths.add(rel)
        parent = Path(rel).parent
        while str(parent) not in {".", "/"}:
            paths.add(str(parent))
            parent = parent.parent
    return frozenset(paths)


def link_targets(text: str) -> list[str]:
    body = _FENCE.sub("", text)
    targets = []
    for match in _LINK.finditer(body):
        target = match.group(1)
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        targets.append(target)
    return targets


def check_file(path: Path, tracked: frozenset[str] | None = None) -> list[Broken]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        return [Broken(path, str(path), f"unreadable: {exc}")]

    broken: list[Broken] = []
    for target in link_targets(text):
        if target.startswith(_SKIP_SCHEMES) or not target:
            continue

        raw = target.split("#", 1)[0]
        if not raw:
            continue

        resolved = (path.parent / raw).resolve()

        if tracked is None:
            if not resolved.exists():
                broken.append(Broken(path, target, "target does not exist"))
            continue

        try:
            rel = str(resolved.relative_to(REPO_ROOT))
        except ValueError:
            broken.append(Broken(path, target, "target escapes the repository"))
            continue

        if rel in tracked or rel.startswith(_ALLOWED_UNTRACKED):
            continue

        reason = (
            "exists locally but is NOT tracked by the public git — "
            "a reader of the published repository cannot open it"
            if resolved.exists()
            else "target does not exist"
        )
        broken.append(Broken(path, target, reason))

    return broken


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="only print failures")
    args = parser.parse_args(argv)

    files = tracked_markdown()
    tracked = tracked_paths()
    broken: list[Broken] = []
    for path in files:
        if path.exists():
            broken.extend(check_file(path, tracked))

    if broken:
        print(f"{len(broken)} broken documentation link(s):\n", file=sys.stderr)
        for item in sorted(broken, key=lambda b: (str(b.source), b.target)):
            print(f"  {item.render()}", file=sys.stderr)
        print(
            "\nEvery link in a public doc must resolve for someone who cloned only "
            "the public repository.",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"all relative links resolve across {len(files)} markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
