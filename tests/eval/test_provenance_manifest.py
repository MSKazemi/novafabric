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
"""ADR-0100 §NF-023 — reproducible-build eval provenance manifest.

A manifest pins the full eval closure (container image, kernel/determinism flags,
seeds, dataset hashes) and content-addresses it, so a third party can rebuild the
closure and a verifier can say **exactly which element diverged** — the "pure
hashing" check the ADR describes. The empirical rebuild-and-reproduce spike
(SPK-DET-5) is a separate, deferred validation.
"""

from __future__ import annotations

from novafabric.eval.provenance_manifest import (
    EvalProvenanceManifest,
    build_eval_manifest,
    verify_eval_closure,
)

_CREATED = "2026-07-25T00:00:00Z"


def _manifest(**over) -> EvalProvenanceManifest:
    base = {
        "suite_id": "mmlu",
        "container_digest": "sha256:" + "a" * 64,
        "kernel_flags": {"batch_invariant_ops": "on", "tf32": "off"},
        "seeds": {"global": 42, "torch": 7},
        "dataset_hashes": {"mmlu": "sha256:" + "b" * 64},
        "created_at": _CREATED,
    }
    base.update(over)
    return build_eval_manifest(**base)  # type: ignore[arg-type]


def _closure(m: EvalProvenanceManifest) -> dict:
    return {
        "container_digest": m.container_digest,
        "kernel_flags": dict(m.kernel_flags),
        "seeds": dict(m.seeds),
        "dataset_hashes": dict(m.dataset_hashes),
    }


class TestManifestDigest:
    def test_same_closure_same_digest(self) -> None:
        a = _manifest()
        b = _manifest(created_at="2099-01-01T00:00:00Z")  # timestamp not part of closure
        assert a.manifest_digest == b.manifest_digest
        assert a.manifest_digest.startswith("sha256:")

    def test_different_closure_differs(self) -> None:
        a = _manifest()
        b = _manifest(seeds={"global": 42, "torch": 8})
        assert a.manifest_digest != b.manifest_digest


class TestClosureMatch:
    def test_identical_closure_matches(self) -> None:
        m = _manifest()
        result = verify_eval_closure(_closure(m), m)
        assert result.matches is True
        assert result.mismatches == []

    def test_seed_divergence_is_pinpointed(self) -> None:
        m = _manifest()
        obs = _closure(m)
        obs["seeds"]["torch"] = 999
        result = verify_eval_closure(obs, m)
        assert result.matches is False
        assert "seed:torch" in result.mismatches

    def test_container_divergence(self) -> None:
        m = _manifest()
        obs = _closure(m)
        obs["container_digest"] = "sha256:" + "c" * 64
        result = verify_eval_closure(obs, m)
        assert result.matches is False
        assert "container_digest" in result.mismatches

    def test_dataset_divergence(self) -> None:
        m = _manifest()
        obs = _closure(m)
        obs["dataset_hashes"]["mmlu"] = "sha256:" + "d" * 64
        result = verify_eval_closure(obs, m)
        assert "dataset:mmlu" in result.mismatches

    def test_missing_dataset_is_a_mismatch(self) -> None:
        m = _manifest()
        obs = _closure(m)
        del obs["dataset_hashes"]["mmlu"]
        result = verify_eval_closure(obs, m)
        assert result.matches is False
        assert any("mmlu" in mm for mm in result.mismatches)

    def test_flag_divergence(self) -> None:
        m = _manifest()
        obs = _closure(m)
        obs["kernel_flags"]["tf32"] = "on"
        result = verify_eval_closure(obs, m)
        assert "flag:tf32" in result.mismatches

    def test_robust_to_partial_observed(self) -> None:
        # A closure missing whole sections still verifies (reports mismatches), never raises.
        m = _manifest()
        result = verify_eval_closure({}, m)
        assert result.matches is False
        assert result.mismatches  # everything is a mismatch, but no exception
