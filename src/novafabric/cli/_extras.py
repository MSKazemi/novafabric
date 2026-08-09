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

"""Optional extras: what they contain, what is missing, and how to install them.

NovaFabric declares 32 optional extras, so "which one do I need?" is the question a
new install runs into first. Two things here answer it.

**Install hints that survive Rich.** ``pip install 'novafabric[serve]'`` printed
through a Rich console renders as ``pip install 'novafabric'`` — Rich reads
``[serve]`` as a markup tag and drops it. The instruction therefore lost the one
token that made it work, at exactly the moment a user needed it, and it did so
silently in five call sites. :func:`rich_install_command` escapes the brackets so
the extra name survives; use it for anything printed through Rich.

**Extras read from installed metadata, never hardcoded.** The set changes, and a
hardcoded copy goes wrong quietly. :func:`declared_extras` and
:func:`extra_requirements` read ``Provides-Extra`` and ``Requires-Dist`` from the
installed distribution. Note the marker uses single quotes
(``extra == 'serve'``); matching only the double-quoted form silently finds
nothing, which is the kind of bug that makes a check look clean while checking
nothing.
"""

from __future__ import annotations

import re
from importlib.metadata import PackageNotFoundError, distribution, metadata

__all__ = [
    "declared_extras",
    "extra_requirements",
    "install_command",
    "missing_requirements",
    "rich_install_command",
]

_DIST = "novafabric"

#: ``name>=1.2; extra == 'serve'`` — the marker quoting varies by build backend,
#: so both forms are accepted rather than assuming one.
_EXTRA_MARKER = re.compile(r"""extra\s*==\s*['"](?P<extra>[^'"]+)['"]""")

#: Leading distribution name of a requirement string, before any version or marker.
_DIST_NAME = re.compile(r"^\s*(?P<name>[A-Za-z0-9._-]+)")


def install_command(extra: str) -> str:
    """The literal shell command that installs ``extra``.

    Quoted because most shells treat ``[`` and ``]`` as glob characters, so an
    unquoted ``pip install novafabric[serve]`` fails or silently installs nothing
    in zsh.
    """
    return f"pip install 'novafabric[{extra}]'"


def rich_install_command(extra: str) -> str:
    """:func:`install_command`, escaped so a Rich console prints it intact.

    Rich strips ``[serve]`` as a markup tag. Escaping the opening bracket is what
    keeps the extra name in the rendered output.
    """
    return install_command(extra).replace("[", r"\[")


def declared_extras() -> list[str]:
    """Every extra the installed distribution declares, sorted.

    Empty when the distribution is not installed — an editable checkout run
    straight from source — rather than raising, so a diagnostic can report the
    situation instead of crashing on it.
    """
    try:
        md = metadata(_DIST)
    except PackageNotFoundError:
        return []
    return sorted(md.get_all("Provides-Extra") or [])


def extra_requirements(extra: str) -> list[str]:
    """Distribution names required by ``extra``, sorted."""
    try:
        md = metadata(_DIST)
    except PackageNotFoundError:
        return []

    names: set[str] = set()
    for req in md.get_all("Requires-Dist") or []:
        marker = _EXTRA_MARKER.search(req)
        if marker is None or marker.group("extra") != extra:
            continue
        name = _DIST_NAME.match(req)
        if name is not None:
            names.add(name.group("name"))
    return sorted(names)


def missing_requirements(extra: str) -> list[str]:
    """Requirements of ``extra`` that are not installed, sorted.

    Asks whether the *distribution* is present rather than whether a module
    imports. A distribution name is not reliably its import name — ``python-louvain``
    imports as ``community`` — so guessing the module produced a false "missing" for
    a package that was installed all along. Telling someone to reinstall what they
    already have is worse than staying quiet, so the accurate check wins.

    It also avoids importing third-party code purely to look at it, which a
    diagnostic has no business doing.
    """
    missing: list[str] = []
    for dist_name in extra_requirements(extra):
        try:
            distribution(dist_name)
        except PackageNotFoundError:
            missing.append(dist_name)
    return sorted(missing)
