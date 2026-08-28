# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Every `force-include` source must also be COPYed into the Docker build context.

`uv build` on a developer machine cannot see this defect: there the repo root *is*
the build context, so a `force-include` entry resolves whatever the Dockerfile
copies. Only Docker's narrower context exposes it, and then only at image build
time — which, before v0.100.1, meant at `v*` tag time.

It has already happened twice:

* 2026-07-24 — `alembic/` gained a force-include with no matching COPY.
* 2026-08-05 (v0.99.0/v0.100.0) — BL-037 added the three canonical JSON Schemas
  with no matching COPY. The image was unbuildable for two releases:
  ``FileNotFoundError: Forced include not found:
  /build/schemas/export-manifest.schema.json``.

The second fix added `release-toolchain.yml`, which builds the image on a pull
request. That is a real guard, but it **cannot catch the third instance**: it is
`paths:`-filtered to `deploy/docker/**`, `deploy/helm/**` and the publish
workflows, while the change that causes this defect is an edit to
`pyproject.toml` that deliberately does *not* touch the Dockerfile. Adding
`pyproject.toml` to that filter (done) helps, but a filter is a heuristic about
which files matter, and this test is the invariant itself — it runs on every
suite run, in milliseconds, without Docker.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = ROOT / "deploy/docker/Dockerfile"
PYPROJECT = ROOT / "pyproject.toml"


def _force_include_sources() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    section = data["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    return sorted(section)


def _builder_copy_sources() -> set[str]:
    """COPY sources in the builder stage, up to the `uv build` that consumes them.

    A COPY after the build cannot satisfy the build, so the cut-off matters.
    """
    lines = DOCKERFILE.read_text(encoding="utf-8").splitlines()
    build_at = next(
        (i for i, ln in enumerate(lines) if ln.strip().startswith("RUN uv build")),
        len(lines),
    )
    sources: set[str] = set()
    for line in lines[:build_at]:
        stripped = line.strip()
        if not stripped.startswith("COPY "):
            continue
        parts = stripped.split()[1:]
        # Skip `COPY --from=...` (other stages/images, not the build context).
        if any(p.startswith("--from=") for p in parts):
            continue
        parts = [p for p in parts if not p.startswith("--")]
        for src in parts[:-1]:  # the last token is the destination
            sources.add(src.rstrip("/"))
    return sources


def test_there_are_force_include_entries_to_check() -> None:
    """Guard the guard: an empty section would make every assertion vacuous."""
    assert len(_force_include_sources()) >= 4, (
        "expected the force-include section to hold at least the alembic entry "
        "and the three canonical JSON Schemas"
    )


def test_the_builder_stage_copies_something() -> None:
    """Guard the guard: an empty COPY set would make the parse silently pass."""
    assert _builder_copy_sources(), (
        "parsed no COPY sources from the builder stage — the parser is broken, "
        "and a broken parser here reports success for every input"
    )


@pytest.mark.parametrize("source", _force_include_sources())
def test_force_include_source_exists_in_the_repo(source: str) -> None:
    assert (ROOT / source).exists(), (
        f"pyproject force-include names {source!r}, which does not exist. "
        "The wheel build fails with 'Forced include not found'."
    )


@pytest.mark.parametrize("source", _force_include_sources())
def test_force_include_source_is_copied_into_the_docker_context(source: str) -> None:
    """The top-level path of every force-include must be COPYed before `uv build`."""
    copies = _builder_copy_sources()
    top = Path(source).parts[0]
    covered = top in copies or source.rstrip("/") in copies
    assert covered, (
        f"pyproject force-includes {source!r}, but nothing in the builder stage "
        f"of deploy/docker/Dockerfile COPYs {top!r} before `RUN uv build`.\n"
        f"COPY sources found: {sorted(copies)}\n"
        "The wheel build inside the image will fail with 'Forced include not "
        f"found: /build/{source}'. Add `COPY {top}/ {top}/` to the builder stage.\n"
        "This has already shipped twice — alembic/ (2026-07-24) and the JSON "
        "Schemas (v0.99.0, which left the image unbuildable for two releases)."
    )


@pytest.mark.parametrize("source", _force_include_sources())
def test_force_include_source_is_not_excluded_by_dockerignore(source: str) -> None:
    """A COPY cannot bring in what `.dockerignore` removed from the context."""
    dockerignore = ROOT / ".dockerignore"
    if not dockerignore.exists():
        pytest.skip(".dockerignore does not exist")
    top = Path(source).parts[0]
    patterns = [
        ln.strip()
        for ln in dockerignore.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    offending = [p for p in patterns if p.rstrip("/") == top and not p.startswith("!")]
    assert not offending, (
        f".dockerignore excludes {offending!r}, which removes {source!r} from the "
        "build context. The COPY will silently bring in nothing and `uv build` "
        "will fail with 'Forced include not found'."
    )
