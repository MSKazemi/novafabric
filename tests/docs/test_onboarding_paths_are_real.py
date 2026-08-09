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

"""The getting-started guide names the directory the CLI actually writes to.

``docs/getting-started.md`` bound ``RUN=.novafabric/capsules/$(ls -t …)`` — a path
*relative to the working directory*. ``nova capture`` writes under
:func:`novafabric._paths.default_capsule_dir` (``~/.novafabric/capsules`` by
default), so on a default install that ``ls`` failed, ``RUN`` resolved to an empty
capsule name, and every step after it (``cat $RUN/capsule.yaml``,
``nova validate $RUN``, ``nova replay $RUN``) failed too. Nothing caught it because
nothing executed the guide.

**There are two capture defaults, and only one belongs in this guide.** The CLI
writes to ``default_capsule_dir()``; the in-process SDK
(``CaptureOrchestrator``, and the adapters and proxies built on it) defaults to
``./.novafabric/runs/`` relative to the working directory
(``capture/orchestrator.py:207``). Both are correct in their own context — see
``docs/README.md`` — but getting-started is a CLI walkthrough, so a ``runs`` path
there sends the reader to a directory their ``nova capture`` never created.

The assertions derive from ``default_capsule_dir()`` rather than restating a path,
so moving the default moves the requirement with it.
"""

from __future__ import annotations

import re
from pathlib import Path

from novafabric._paths import default_capsule_dir

_ROOT = Path(__file__).resolve().parents[2]
_GETTING_STARTED = _ROOT / "docs" / "getting-started.md"

#: The leaf of the real capsule directory, derived rather than restated.
_CAPSULE_LEAF = default_capsule_dir().name

_RUN_ASSIGNMENT = re.compile(r"^RUN=(?P<value>\S+)", re.M)


def test_getting_started_binds_run_to_an_absolute_capsule_path() -> None:
    """A relative ``.novafabric/capsules`` is the exact bug this guards.

    The working directory is not where capsules live, so a bare relative path only
    works if the reader happens to have run from their home directory.
    """
    text = _GETTING_STARTED.read_text(encoding="utf-8")
    assignments = [m.group("value") for m in _RUN_ASSIGNMENT.finditer(text)]

    assert assignments, "no RUN= assignment found — the guide's shape changed"

    for value in assignments:
        assert _CAPSULE_LEAF in value, (
            f"RUN={value} does not name the {_CAPSULE_LEAF!r} directory that "
            f"default_capsule_dir() returns"
        )
        assert value.startswith(("~", "/", "$")), (
            f"RUN={value} is relative to the working directory. Capsules are written "
            f"under {default_capsule_dir()}, so this only resolves if the reader "
            f"happens to be in the right directory — which is how this broke before."
        )


def test_getting_started_does_not_send_cli_readers_to_the_sdk_directory() -> None:
    """``.novafabric/runs/`` is the SDK default, not the CLI's.

    It is a real directory — ``CaptureOrchestrator`` creates it — but only for
    in-process capture. A CLI walkthrough that names it points the reader at a
    directory their ``nova capture`` never wrote to.
    """
    text = _GETTING_STARTED.read_text(encoding="utf-8")

    assert ".novafabric/runs" not in text, (
        "getting-started.md is a CLI walkthrough but names .novafabric/runs/, which "
        f"is the in-process SDK default. `nova capture` writes to {default_capsule_dir()}"
    )
