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

"""The C2PA binder reads only inside the capsule (ADR-0148 D1, confined reads).

``blob_ref`` comes from ``model-calls.jsonl``, which the recorded workload writes.
``_blob_bytes_for`` used to do ``(capsule_dir / blob_ref).read_bytes()``: an absolute
path, a ``..``, a link or a device was followed, and the read was unbounded. A blob
read outside the capsule could even make a binding *pass* against bytes the capsule
does not hold. Now an invalid ref is refused, recorded in the facet's
``blob_refs_refused``, and the binding falls back to the recorded hash.
"""

from __future__ import annotations

import json
import os
import signal
from collections.abc import Iterator
from pathlib import Path

import pytest

from novafabric.trust.provenance import c2pa_bind
from novafabric.trust.provenance.c2pa_bind import (
    SIDECAR_SUFFIX,
    build_facet,
    discover_sidecar_manifests,
)
from trust._provenance_fixtures import IMAGE_BYTES, IMAGE_HEX, a_capsule, a_manifest


@pytest.fixture()
def no_hang() -> Iterator[None]:
    def _alarm(_signum: int, _frame: object) -> None:
        raise TimeoutError("the binder blocked on a FIFO")

    previous = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(10)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _set_blob_ref(capsule: Path, blob_ref: str) -> None:
    path = capsule / "model-calls.jsonl"
    record = json.loads(path.read_text())
    record["gen_ai.request.messages"][0]["content"][0]["media"]["blob_ref"] = blob_ref
    path.write_text(json.dumps(record) + "\n")


def _outside_blob(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / "image.png"
    path.parent.mkdir()
    path.write_bytes(IMAGE_BYTES)  # bytes that WOULD make the binding pass
    return path


def _capsule_without_blob(tmp_path: Path) -> Path:
    return a_capsule(tmp_path, blob=None, sidecar=a_manifest())


def test_a_relative_escape_is_refused_and_recorded(tmp_path: Path) -> None:
    _outside_blob(tmp_path)
    capsule = _capsule_without_blob(tmp_path)
    _set_blob_ref(capsule, "../outside/image.png")
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "recorded_hash"
    assert facet.blob_refs_refused is not None
    assert facet.blob_refs_refused[0].startswith("../outside/image.png")


def test_an_absolute_path_is_refused(tmp_path: Path) -> None:
    outside = _outside_blob(tmp_path)
    capsule = _capsule_without_blob(tmp_path)
    _set_blob_ref(capsule, str(outside))
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "recorded_hash"
    assert facet.blob_refs_refused and facet.blob_refs_refused[0].startswith(str(outside))


def test_a_symlinked_blob_is_refused(tmp_path: Path) -> None:
    outside = _outside_blob(tmp_path)
    capsule = _capsule_without_blob(tmp_path)
    (capsule / "outputs" / f"{IMAGE_HEX}.png").symlink_to(outside)
    _set_blob_ref(capsule, f"outputs/{IMAGE_HEX}.png")
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "recorded_hash"
    assert facet.blob_refs_refused and "symlink" in facet.blob_refs_refused[0]


def test_a_fifo_blob_is_refused_without_blocking(tmp_path: Path, no_hang: None) -> None:
    capsule = _capsule_without_blob(tmp_path)
    os.mkfifo(capsule / "outputs" / f"{IMAGE_HEX}.png")
    _set_blob_ref(capsule, f"outputs/{IMAGE_HEX}.png")
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "recorded_hash"
    assert facet.blob_refs_refused


def test_an_oversize_blob_is_refused_not_read_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    monkeypatch.setattr(c2pa_bind, "_max_blob_bytes", lambda: 4)
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "recorded_hash"
    assert facet.blob_refs_refused and "larger than 4 bytes" in facet.blob_refs_refused[0]


def test_a_missing_blob_is_not_a_refusal(tmp_path: Path) -> None:
    capsule = _capsule_without_blob(tmp_path)
    _set_blob_ref(capsule, f"outputs/{IMAGE_HEX}.png")  # recorded, never stored
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "recorded_hash"
    assert facet.blob_refs_refused is None


def test_a_good_blob_still_binds_against_its_bytes(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "blob_bytes"
    assert facet.blob_refs_refused is None
    assert "blob_refs_refused" not in facet.model_dump(exclude_none=True)


def test_a_symlinked_sidecar_is_never_read(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=None)
    outside = tmp_path / "outside-manifest.json"
    outside.write_text(json.dumps(a_manifest()))
    (capsule / "outputs" / f"{IMAGE_HEX}{SIDECAR_SUFFIX}").symlink_to(outside)
    assert discover_sidecar_manifests(capsule) == {}
