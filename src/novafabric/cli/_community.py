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

"""The first-run community invitation: text the user reads, never a network call.

People install NovaFabric, seal a capsule, and the tool never once mentions that a
place to talk about it exists. This module is the whole of the fix, and its limits
are the point:

* **Pull, never ping.** It prints a URL. It opens no socket, writes no state file,
  and checks for no update — no telemetry is a house rule, not a preference.
* **Once, not every time.** ``nova capture`` shows it only when the capsule it just
  wrote is the *first* one in its directory; that is derived from the directory
  itself, so there is no marker file to create or forget.
* **Never in a pipe.** Callers print it only to an interactive terminal, and
  ``nova --version`` sends it to stderr so ``$(nova --version)`` stays one line.
* **Off switch.** ``NOVAFABRIC_COMMUNITY_HINT=0`` silences it everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

DISCUSSIONS_URL = "https://github.com/MSKazemi/novafabric/discussions"

HINT_ENV = "NOVAFABRIC_COMMUNITY_HINT"

FIRST_CAPSULE_INVITE = (
    f"First capsule sealed. Show us what you captured, or ask anything: {DISCUSSIONS_URL}"
)
VERSION_INVITE = f"Questions, ideas, show-and-tell: {DISCUSSIONS_URL}"


def hint_enabled() -> bool:
    """False when the user set ``NOVAFABRIC_COMMUNITY_HINT=0``."""
    return os.environ.get(HINT_ENV, "1") != "0"


def is_first_capsule(capsule_dir: Path) -> bool:
    """True when *capsule_dir* is the only capsule in its parent directory.

    A capsule is a sub-directory holding ``capsule.yaml``; anything else in the
    parent (replays, stray files) is ignored. The scan stops at the second capsule,
    so a directory holding thousands costs the same as one holding two.
    """
    parent = capsule_dir.parent
    seen = 0
    try:
        for entry in parent.iterdir():
            if entry.is_dir() and (entry / "capsule.yaml").is_file():
                seen += 1
                if seen > 1:
                    return False
    except OSError:
        return False
    return seen == 1
