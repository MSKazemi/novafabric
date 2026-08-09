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

"""Resolve a capsule reference that is either a path or a bare run id.

``nova capture`` ends by printing ``(run_id=01KZ…)``, so the run id is the thing a
user has in hand and the thing they paste into the next command. Until now every
capsule-taking command accepted only a directory path, which meant the documented
first run did not work: the README's ``nova replay <run_id> --mode forensic`` failed
with "Not a valid capsule directory", and ``docs/getting-started.md`` had to
reconstruct the path with ``RUN=.novafabric/capsules/$(ls -t …)`` — a line that
resolved to nothing, because capsules are written under ``$HOME`` and that path is
relative to the working directory.

Accepting the id directly removes the reconstruction step rather than documenting it
more carefully.

**A path always wins.** If the reference names an existing capsule directory it is
used as given, and no lookup happens. That ordering matters: it keeps every existing
invocation byte-identical in behaviour, so this widens the accepted input without
changing any input that already worked. Only a reference that is *not* a usable path
is treated as an id.
"""

from __future__ import annotations

from pathlib import Path

from novafabric._paths import default_capsule_dir

__all__ = ["CapsuleRefError", "resolve_capsule_ref"]


class CapsuleRefError(ValueError):
    """A capsule reference matched neither a capsule directory nor a known run id.

    Carries the rendered, user-facing message; callers print it and exit non-zero
    rather than re-deriving the wording.
    """


def _is_capsule_dir(path: Path) -> bool:
    return path.is_dir() and (path / "capsule.yaml").is_file()


def resolve_capsule_ref(ref: str | Path, *, capsule_dir: Path | None = None) -> Path:
    """Return the capsule directory for ``ref``.

    ``ref`` may be a path to a capsule directory (absolute or relative), or a bare
    run id resolved against ``capsule_dir`` — by default
    :func:`novafabric._paths.default_capsule_dir`, so ``NOVAFABRIC_CAPSULE_DIR`` and
    ``NOVAFABRIC_HOME`` are honoured without the caller thinking about it.

    Raises:
        CapsuleRefError: with a message naming the directory that was searched.
    """
    candidate = Path(ref)

    # A usable path wins outright — never look up an id when the user gave a path.
    if _is_capsule_dir(candidate):
        return candidate

    # Only a single path segment can be a run id; "a/b" is a path the user got wrong,
    # and reporting it as an unknown id would be actively misleading.
    looks_like_id = candidate.parent == Path(".") and candidate.name == str(ref)

    base = capsule_dir if capsule_dir is not None else default_capsule_dir()
    if looks_like_id:
        by_id = base / candidate.name
        if _is_capsule_dir(by_id):
            return by_id

    raise CapsuleRefError(_not_found_message(str(ref), base, looks_like_id=looks_like_id))


def _not_found_message(ref: str, base: Path, *, looks_like_id: bool) -> str:
    """Say which of the two lookups failed, and where to look.

    A bare "not a valid capsule directory" gives no way forward when the user pasted
    the run id the tool itself just printed. The hint names the directory rather than
    a command, because there is no single "list my capsules" subcommand to point at
    and a hint that names a command which does not exist is worse than none.
    """
    if not looks_like_id:
        return f"No capsule at path: {ref}"

    return (
        f"No capsule found for '{ref}'.\n"
        f"  Not a directory here, and no run with that id in: {base}\n"
        f"  Captured runs live there — check the id, or set NOVAFABRIC_CAPSULE_DIR "
        f"if they are stored elsewhere."
    )
