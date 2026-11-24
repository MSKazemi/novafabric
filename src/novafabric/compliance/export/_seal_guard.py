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
"""Fail-closed seal check shared by the exporters that list a seal as a control.

:func:`capsule_seal_verifies` answers ``False`` both for a capsule that was never
sealed and for one whose ``.seal`` exists but no longer verifies. An exporter that
only asked that Boolean treated a sealed-then-edited capsule as merely unsealed and
rendered the edited bytes as ordinary evidence. This guard asks the tri-state
:func:`capsule_seal_status` instead:

* ``absent``  — never sealed: the export proceeds and stays visibly unsigned;
* ``valid``   — the bytes the seal names are unchanged since sealing: the export
  may list the seal (it does not say who signed them);
* ``invalid`` — a seal is present and fails: :class:`CapsuleSealInvalidError` is
  raised, so no document is built from bytes the seal contradicts.

A document that claims *log* tamper-evidence needs more than ``valid``: a seal made
without ``evidence_digests`` signs the manifest only, and a stream added after sealing
is not named by the seal, so neither can detect a change to the evidence streams.
:func:`seal_for_log_claims` separates that ``unbound`` case from the others.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from novafabric.trust.novaseal.capsule_verify import capsule_seal_status

from .provenance import CAPSULE_REF_FILES


class CapsuleSealInvalidError(ValueError):
    """A capsule carries a ``.seal`` that fails verification; refuse to export it."""

    def __init__(self, capsule_dir: Path) -> None:
        self.capsule_dir = capsule_dir
        super().__init__(
            f"{capsule_dir}: the capsule carries a .seal that failed seal verification "
            "(edited after sealing, or a damaged, substituted or unreadable seal); "
            "refusing to export evidence from it — "
            f"run `nova verify {capsule_dir}` for the reasons"
        )


def seal_is_valid_or_refuse(capsule_dir: Path) -> bool:
    """``True`` for a valid seal, ``False`` for none; raise for a broken one."""
    status = capsule_seal_status(capsule_dir)
    if status == "invalid":
        raise CapsuleSealInvalidError(capsule_dir)
    return status == "valid"


def all_seals_valid_or_refuse(capsule_dirs: Iterable[Path]) -> bool:
    """``True`` only if every capsule holds a valid seal; raise if *any* seal is broken.

    Every directory is checked before the answer is given, so one unsealed capsule
    cannot hide a broken seal in another.
    """
    results = [seal_is_valid_or_refuse(d) for d in capsule_dirs]
    return all(results)


LogSealState = Literal["absent", "bound", "unbound"]


def seal_for_log_claims(capsule_dir: Path) -> LogSealState:
    """What a seal can honestly say about the capsule's evidence streams.

    * ``"absent"`` — never sealed;
    * ``"bound"`` — a valid seal that also names every file a capsule reference
      digests (:data:`CAPSULE_REF_FILES`), so a change to any of them is detectable;
    * ``"unbound"`` — a valid seal that does not bind those streams (it predates
      evidence digests, or a digested stream was added after sealing). Nothing it
      vouches for was changed, so the export is not refused, but it must not claim
      the logs are tamper-evident.

    Raises :class:`CapsuleSealInvalidError` for a seal that fails verification.
    """
    if not seal_is_valid_or_refuse(capsule_dir):
        return "absent"
    covered = capsule_seal_status(capsule_dir, must_cover=CAPSULE_REF_FILES)
    return "bound" if covered == "valid" else "unbound"
