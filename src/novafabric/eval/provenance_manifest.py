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
"""Reproducible-build eval provenance manifest (ADR-0100 §NF-023).

A :class:`EvalProvenanceManifest` pins the full closure an eval suite ran against
— the container image digest, the kernel/determinism flags (e.g. batch-invariant
ops, TF32), the seeds, and the dataset+split hashes — and **content-addresses that
closure**. It is an Evidence Bundle payload (in-toto/DSSE-wrappable), not a new
top-level format, and composes with the eval-card provenance of ADR-0099.

The manifest is the "reproducible build as copyleft" carrier: a third party can
rebuild the closure from it, and :func:`verify_eval_closure` — a **pure hashing /
comparison** check, no GPU or execution — reports **exactly which closure element
diverged** (which seed, dataset, flag, or the container). This is the verifiable
half; the empirical rebuild-and-reproduce validation (SPK-DET-5) is separate and
deferred, as are the GPU-gated batch-invariance attestations (NF-012/013/014).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field


def _digest_closure(
    *,
    suite_id: str,
    container_digest: str,
    kernel_flags: dict[str, str],
    seeds: dict[str, int],
    dataset_hashes: dict[str, str],
) -> str:
    """Content-address the eval closure (order-independent; timestamp excluded)."""
    canonical = {
        "suite_id": suite_id,
        "container_digest": container_digest,
        "kernel_flags": dict(sorted(kernel_flags.items())),
        "seeds": dict(sorted(seeds.items())),
        "dataset_hashes": dict(sorted(dataset_hashes.items())),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


class EvalProvenanceManifest(BaseModel):
    """A sealed manifest pinning one eval suite's full reproducibility closure."""

    model_config = {"frozen": True}

    suite_id: str = Field(..., description="Eval suite identifier, e.g. 'mmlu'")
    container_digest: str = Field(..., description="Digest of the eval container image")
    kernel_flags: dict[str, str] = Field(
        default_factory=dict,
        description="Determinism/kernel flags (batch-invariant ops, TF32, …)",
    )
    seeds: dict[str, int] = Field(default_factory=dict, description="Named RNG seeds")
    dataset_hashes: dict[str, str] = Field(
        default_factory=dict, description="Dataset/split name → content hash"
    )
    created_at: str = Field(
        ..., description="ISO-8601 UTC manifest creation time (not part of the closure)"
    )
    manifest_digest: str = Field(
        ..., description="Content-address of the closure (excludes created_at)"
    )


class ClosureMatch(BaseModel):
    """The result of comparing an observed run's closure to a sealed manifest."""

    model_config = {"frozen": True}

    matches: bool
    mismatches: list[str] = Field(
        default_factory=list,
        description="Diverging elements, e.g. 'seed:torch', 'dataset:mmlu', 'container_digest'",
    )


def build_eval_manifest(
    *,
    suite_id: str,
    container_digest: str,
    created_at: str,
    kernel_flags: dict[str, str] | None = None,
    seeds: dict[str, int] | None = None,
    dataset_hashes: dict[str, str] | None = None,
) -> EvalProvenanceManifest:
    """Assemble a manifest and compute its closure content-address."""
    kernel_flags = dict(kernel_flags or {})
    seeds = dict(seeds or {})
    dataset_hashes = dict(dataset_hashes or {})
    digest = _digest_closure(
        suite_id=suite_id,
        container_digest=container_digest,
        kernel_flags=kernel_flags,
        seeds=seeds,
        dataset_hashes=dataset_hashes,
    )
    return EvalProvenanceManifest(
        suite_id=suite_id,
        container_digest=container_digest,
        kernel_flags=kernel_flags,
        seeds=seeds,
        dataset_hashes=dataset_hashes,
        created_at=created_at,
        manifest_digest=digest,
    )


def verify_eval_closure(
    observed: dict[str, Any],
    manifest: EvalProvenanceManifest,
) -> ClosureMatch:
    """Compare an *observed* run closure to *manifest*; pinpoint every divergence.

    *observed* is a plain dict with any of the keys ``container_digest``,
    ``kernel_flags``, ``seeds``, ``dataset_hashes``. Missing sections count as
    mismatches for every element the manifest pins. Pure comparison — never
    executes anything and never raises.
    """
    mismatches: list[str] = []

    if observed.get("container_digest") != manifest.container_digest:
        mismatches.append("container_digest")

    obs_flags = observed.get("kernel_flags") or {}
    for name, flag_value in manifest.kernel_flags.items():
        if str(obs_flags.get(name)) != str(flag_value):
            mismatches.append(f"flag:{name}")

    obs_seeds = observed.get("seeds") or {}
    for name, seed_value in manifest.seeds.items():
        if obs_seeds.get(name) != seed_value:
            mismatches.append(f"seed:{name}")

    obs_datasets = observed.get("dataset_hashes") or {}
    for name, ds_hash in manifest.dataset_hashes.items():
        if obs_datasets.get(name) != ds_hash:
            mismatches.append(f"dataset:{name}")

    return ClosureMatch(matches=not mismatches, mismatches=mismatches)


__all__ = [
    "ClosureMatch",
    "EvalProvenanceManifest",
    "build_eval_manifest",
    "verify_eval_closure",
]
