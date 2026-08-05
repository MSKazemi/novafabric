"""Every wheel ``force-include`` source must exist in the Docker build context.

Twice now a ``[tool.hatch.build.targets.wheel.force-include]`` entry has landed
without a matching ``COPY`` in ``deploy/docker/Dockerfile``, and both times the
container image was silently unbuildable until someone tried to release it:

* 2026-07-24 — ``alembic/`` was force-included with no COPY. The image had been
  unbuildable for weeks; found while verifying something else.
* v0.99.0 — the canonical JSON Schemas (BL-037) were force-included with no COPY.
  The image failed at the release tag with
  ``FileNotFoundError: Forced include not found: /build/schemas/…``.

The failure mode is nasty because it is invisible everywhere except a container
build: ``uv build`` on a developer machine succeeds, because the repo root *is*
the build context there. Only Docker's narrower context exposes it.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "deploy" / "docker" / "Dockerfile"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# `COPY <src>... <dest>` — capture the sources, ignoring --from=… stages, which
# copy out of a previous build stage rather than out of the context.
_COPY = re.compile(r"^COPY\s+(?!--from=)(?P<args>.+)$", re.MULTILINE)


def _force_include_sources() -> list[str]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    wheel = data.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {}).get("wheel", {})
    return sorted(wheel.get("force-include", {}))


def _context_paths() -> list[str]:
    text = _DOCKERFILE.read_text(encoding="utf-8")
    paths: list[str] = []
    for match in _COPY.finditer(text):
        args = match.group("args").split()
        # The last token is the destination.
        paths.extend(args[:-1])
    return paths


@pytest.mark.skipif(not _DOCKERFILE.exists(), reason="Dockerfile not present")
def test_every_force_include_source_is_copied_into_the_build_context() -> None:
    copied = _context_paths()
    missing: list[str] = []

    for source in _force_include_sources():
        top = source.split("/", 1)[0]
        # A COPY of `schemas/` covers `schemas/a/b.json`; a COPY of the exact
        # file covers it too.
        if any(c.rstrip("/") in {top, source} for c in copied):
            continue
        missing.append(source)

    assert not missing, (
        "these wheel force-include sources are not COPYed into the Docker build "
        "context, so `uv build` inside the image fails with 'Forced include not "
        f"found': {missing}. Add a COPY to deploy/docker/Dockerfile."
    )


def test_every_force_include_source_actually_exists() -> None:
    """A force-include naming a path that does not exist breaks every build."""
    missing = [s for s in _force_include_sources() if not (_REPO_ROOT / s).exists()]

    assert not missing, f"force-include sources missing from the repository: {missing}"
