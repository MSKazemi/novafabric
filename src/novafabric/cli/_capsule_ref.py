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

**Fallback locations (bounded, explicit).** ``nova capture`` writes under
:func:`~novafabric._paths.default_capsule_dir`, but the framework adapters, the
in-process ``CaptureOrchestrator`` and the proxies write a project-local
``./.novafabric/runs/`` (adapters: ``$NOVAFABRIC_HOME/runs`` when that is set) —
the documented SDK default (``docs/README.md`` "Default storage paths"). So when the
caller did not name a capsule store, a bare id is also looked up in
:func:`~novafabric._paths.adapter_default_runs_dir` and
:func:`~novafabric._paths.project_runs_dir`. At most three directories, never a
filesystem walk. A hit outside the configured store is announced on stderr; an id
found in more than one of them is an error naming every match, never a silent pick.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from novafabric._paths import adapter_default_runs_dir, default_capsule_dir, project_runs_dir

__all__ = ["AmbiguousCapsuleRefError", "CapsuleRefError", "resolve_capsule_ref"]


class CapsuleRefError(ValueError):
    """A capsule reference matched neither a capsule directory nor a known run id.

    Carries the rendered, user-facing message; callers print it and exit non-zero
    rather than re-deriving the wording.
    """


class AmbiguousCapsuleRefError(CapsuleRefError):
    """A bare run id names a capsule in more than one searched directory.

    A subclass, so every caller that already reports :class:`CapsuleRefError`
    reports this too — but distinguishable by callers (``nova validate``) that
    swallow a plain miss to try another interpretation of the argument. Picking
    one copy silently would let two commands read two different capsules.
    """

    def __init__(self, message: str, matches: list[Path]) -> None:
        super().__init__(message)
        self.matches = matches


def _is_capsule_dir(path: Path) -> bool:
    return path.is_dir() and (path / "capsule.yaml").is_file()


def _stderr_notice(message: str) -> None:
    sys.stderr.write(message + "\n")


def _search_dirs(capsule_dir: Path | None) -> list[Path]:
    """The bounded list of stores a bare id is looked up in, primary first.

    An explicit ``capsule_dir`` (e.g. a ``--capsule-dir`` option) is the only
    store searched: the user said where to look. Otherwise the configured store,
    then the two adapter/SDK defaults, de-duplicated by resolved path so a store
    configured *as* ``./.novafabric/runs`` is not reported as ambiguous with itself.
    """
    if capsule_dir is not None:
        return [capsule_dir]
    dirs: list[Path] = []
    seen: set[Path] = set()
    for d in (default_capsule_dir(), adapter_default_runs_dir(), project_runs_dir()):
        key = d.resolve()
        if key not in seen:
            seen.add(key)
            dirs.append(d)
    return dirs


def resolve_capsule_ref(
    ref: str | Path,
    *,
    capsule_dir: Path | None = None,
    notify: Callable[[str], None] = _stderr_notice,
) -> Path:
    """Return the capsule directory for ``ref``.

    ``ref`` may be a path to a capsule directory (absolute or relative), or a bare
    run id resolved against ``capsule_dir`` — by default
    :func:`novafabric._paths.default_capsule_dir`, so ``NOVAFABRIC_CAPSULE_DIR`` and
    ``NOVAFABRIC_HOME`` are honoured without the caller thinking about it, then the
    adapter/SDK default directories (see the module docstring). ``notify`` receives
    one line when the capsule was found outside the configured store.

    Raises:
        AmbiguousCapsuleRefError: the id exists in more than one searched store.
        CapsuleRefError: with a message naming every directory that was searched.
    """
    candidate = Path(ref)

    # A usable path wins outright — never look up an id when the user gave a path.
    if _is_capsule_dir(candidate):
        return candidate

    # Only a single path segment can be a run id; "a/b" is a path the user got wrong,
    # and reporting it as an unknown id would be actively misleading.
    looks_like_id = candidate.parent == Path(".") and candidate.name == str(ref)

    searched = _search_dirs(capsule_dir)
    if looks_like_id:
        matches = [d / candidate.name for d in searched if _is_capsule_dir(d / candidate.name)]
        if len(matches) > 1:
            raise AmbiguousCapsuleRefError(_ambiguous_message(str(ref), matches), matches)
        if matches:
            found = matches[0]
            if found.parent != searched[0]:
                notify(
                    f"note: run {candidate.name} found in {found.parent} "
                    f"(the adapter/SDK default), not in the configured capsule "
                    f"directory {searched[0]}"
                )
            return found

    raise CapsuleRefError(_not_found_message(str(ref), searched, looks_like_id=looks_like_id))


def _ambiguous_message(ref: str, matches: list[Path]) -> str:
    listed = "\n".join(f"    {m}" for m in matches)
    return (
        f"Run id '{ref}' is ambiguous — a capsule with that id exists in "
        f"{len(matches)} places:\n{listed}\n"
        f"  Pass the capsule directory path instead of the id."
    )


def _not_found_message(ref: str, searched: list[Path], *, looks_like_id: bool) -> str:
    """Say which of the two lookups failed, and where to look.

    A bare "not a valid capsule directory" gives no way forward when the user pasted
    the run id the tool itself just printed. The hint names the directories rather
    than a command, because there is no single "list my capsules" subcommand to point
    at and a hint that names a command which does not exist is worse than none.
    """
    if not looks_like_id:
        return f"No capsule at path: {ref}"

    base, *fallbacks = searched
    also = "".join(f"\n  Also searched (adapter/SDK default): {d}" for d in fallbacks)
    return (
        f"No capsule found for '{ref}'.\n"
        f"  Not a directory here, and no run with that id in: {base}"
        f"{also}\n"
        f"  Captured runs live there — check the id, pass the capsule directory path, "
        f"or set NOVAFABRIC_CAPSULE_DIR if they are stored elsewhere."
    )
