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

"""Split the suite into N duration-balanced shards, one test FILE at a time.

CI's ``unit`` job ran ~20K tests as one job, 31-57 min per run (ADR-0316). This
plugin lets N jobs each run a disjoint slice: ``pytest --shard=2/6``.

Why by file, not by test
------------------------
Module-scoped fixtures and the ``xdist_group`` that pins a module's testcontainer
to one worker both assume the whole module runs in one process tree. Splitting a
module across shards would start its container twice and run its module fixtures
twice. A file is therefore the unit of assignment.

Balancing
---------
Files are placed largest-first onto the currently lightest shard (LPT greedy),
weighted by ``tests/.test-durations.json`` — per-file seconds refreshed from CI's
JUnit reports. A file with no recorded duration is weighted at its test count
times the mean seconds-per-test of the files that have one, so a stale durations
file costs balance, never correctness: every file still lands in exactly one
shard. Completeness is additionally proven in CI by comparing the union of the
shard manifests (``--shard-manifest``) against a full collection.
"""

from __future__ import annotations

import heapq
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

__all__ = [
    "DURATIONS_FILE",
    "ShardPlugin",
    "ShardSpec",
    "ShardSpecError",
    "assign",
    "file_of",
    "load_durations",
    "select",
    "weights",
]

DURATIONS_FILE = Path(__file__).resolve().parent / ".test-durations.json"

_SPEC = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s*$")


class ShardSpecError(ValueError):
    """A ``--shard`` value or a durations file that cannot be used."""


@dataclass(frozen=True)
class ShardSpec:
    """Shard *index* (1-based) of *total*."""

    index: int
    total: int

    @classmethod
    def parse(cls, text: str) -> ShardSpec:
        match = _SPEC.match(text)
        if not match:
            raise ShardSpecError(f"--shard expects I/N (e.g. 2/6), got {text!r}")
        index, total = int(match.group(1)), int(match.group(2))
        if total < 1 or not 1 <= index <= total:
            raise ShardSpecError(f"--shard {text!r}: need 1 <= I <= N and N >= 1")
        return cls(index, total)


def file_of(nodeid: str) -> str:
    """The test file a pytest node id belongs to."""
    return nodeid.split("::", 1)[0]


def load_durations(path: Path = DURATIONS_FILE) -> dict[str, float]:
    """Per-file seconds from *path*; an absent file means no timing data yet.

    A present but malformed file raises: it is committed, so silently ignoring
    it would hide a broken refresh behind a quietly worse balance.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        files = data["files"]
        return {str(name): float(seconds) for name, seconds in files.items()}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ShardSpecError(f"{path}: not a durations file ({exc})") from exc


def weights(counts: Mapping[str, int], durations: Mapping[str, float]) -> dict[str, float]:
    """Weight every file in *counts* (file -> number of tests) for balancing."""
    known = [f for f in counts if f in durations]
    known_tests = sum(counts[f] for f in known)
    per_test = sum(durations[f] for f in known) / known_tests if known_tests else 1.0
    return {f: durations[f] if f in durations else counts[f] * per_test for f in counts}


def assign(file_weights: Mapping[str, float], total: int) -> dict[str, int]:
    """Map each file to a 1-based shard, deterministically, heaviest first."""
    if total < 1:
        raise ShardSpecError(f"cannot split into {total} shards")
    # Ties are broken by name and shard index so every process — each xdist
    # worker collects independently — computes the identical assignment.
    order = sorted(file_weights, key=lambda f: (-file_weights[f], f))
    heap = [(0.0, shard) for shard in range(1, total + 1)]
    placement: dict[str, int] = {}
    for name in order:
        load, shard = heapq.heappop(heap)
        placement[name] = shard
        heapq.heappush(heap, (load + file_weights[name], shard))
    return placement


def select(nodeids: Iterable[str], spec: ShardSpec, durations: Mapping[str, float]) -> set[str]:
    """The node ids that belong to shard *spec*."""
    ids = list(nodeids)
    counts = Counter(file_of(n) for n in ids)
    placement = assign(weights(counts, durations), spec.total)
    return {n for n in ids if placement[file_of(n)] == spec.index}


class ShardPlugin:
    """Deselect everything outside ``--shard`` and record what this shard ran.

    Registered by ``tests/conftest.py``. Runs ``trylast`` so it sees the final
    item list — after ``-m``/``-k`` deselection and after the container marker
    hook — and so its manifest is exactly what this shard executes.
    """

    def __init__(self, spec: ShardSpec | None, manifest: Path | None) -> None:
        self.spec = spec
        self.manifest = manifest

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(
        self, config: pytest.Config, items: list[pytest.Item]
    ) -> None:
        if self.spec is not None and self.spec.total > 1:
            keep = select((item.nodeid for item in items), self.spec, load_durations())
            dropped = [item for item in items if item.nodeid not in keep]
            items[:] = [item for item in items if item.nodeid in keep]
            config.hook.pytest_deselected(items=dropped)
        # Every xdist worker collects the same items; one copy is enough.
        worker = getattr(config, "workerinput", {}).get("workerid")
        if self.manifest is not None and worker in (None, "gw0"):
            self.manifest.parent.mkdir(parents=True, exist_ok=True)
            self.manifest.write_text(
                "".join(f"{item.nodeid}\n" for item in items), encoding="utf-8"
            )
