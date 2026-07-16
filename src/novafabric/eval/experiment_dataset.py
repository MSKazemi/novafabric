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

"""Local JSONL dataset loader for the ADR-0120 dataset-experiment harness.

A dataset is one local JSONL file — one item per line, each an object with a
stable ``item_id`` (the A/B alignment key), a free-form ``input``, and an
optional ``expected`` reference output for the built-in zero-token exact-match
scorer. Loading pins the dataset by content:

- ``dataset_hash`` — ``sha256:`` over the raw file bytes (the same content-hash
  discipline as the NF-058 dataset provenance cards and the ADR-0108 facet);
- ``split_hash`` — ``sha256:`` over the canonical JSON array of ``item_id``s
  actually iterated (v0 iterates the whole file, so the split *is* the file's
  item sequence).

Fully local and offline; validation is fail-closed with line-numbered errors.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from novafabric.eval.experiment import DatasetRef


class DatasetError(Exception):
    """A dataset file is missing, malformed, or violates an item invariant."""


class DatasetItem(BaseModel):
    """One dataset item (one JSONL line)."""

    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1)
    input: Any = None
    expected: str | None = None
    metadata: dict[str, Any] | None = None

    def input_text(self) -> str:
        """The item input as text (non-strings rendered as canonical JSON)."""
        if isinstance(self.input, str):
            return self.input
        return json.dumps(self.input, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class LoadedDataset:
    """A validated dataset plus its pinned :class:`DatasetRef`."""

    path: Path
    items: tuple[DatasetItem, ...]
    dataset_ref: DatasetRef


def _split_hash(item_ids: list[str]) -> str:
    canonical = json.dumps(item_ids, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def load_dataset(
    path: str | Path,
    *,
    name: str | None = None,
    version: str | None = None,
) -> LoadedDataset:
    """Load and validate a JSONL dataset, pinning its content hashes.

    *name* defaults to the file stem; *version* defaults to the first 12 hex
    chars of the dataset content hash (so an unlabeled dataset is still pinned
    to exact content, never to a mutable label). Raises :class:`DatasetError`
    on a missing file, invalid JSON, an invalid item, a duplicate ``item_id``,
    or an empty dataset.
    """
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as exc:
        raise DatasetError(f"cannot read dataset file {p}: {exc}") from exc
    dataset_hash = "sha256:" + hashlib.sha256(raw).hexdigest()

    items: list[DatasetItem] = []
    seen: set[str] = set()
    for line_no, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"{p}:{line_no}: invalid JSON: {exc}") from exc
        try:
            item = DatasetItem.model_validate(obj)
        except ValidationError as exc:
            raise DatasetError(f"{p}:{line_no}: invalid dataset item: {exc}") from exc
        if item.item_id in seen:
            raise DatasetError(f"{p}:{line_no}: duplicate item_id {item.item_id!r}")
        seen.add(item.item_id)
        items.append(item)

    if not items:
        raise DatasetError(f"{p}: dataset contains no items")

    ref = DatasetRef(
        name=name or p.stem,
        version=version or dataset_hash.removeprefix("sha256:")[:12],
        dataset_hash=dataset_hash,
        split_hash=_split_hash([item.item_id for item in items]),
    )
    return LoadedDataset(path=p, items=tuple(items), dataset_ref=ref)
