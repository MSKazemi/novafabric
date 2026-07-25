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
"""ADR-0107 §NF-094 — Art. 50 marking log + dual-layer C2PA/SynthID receipt.

NovaFabric logs each AI-disclosure/marking event and builds a *dual-layer* provenance
receipt: the shipped C2PA manifest (ADR-0074) with a **SynthID-presence assertion**
carried *inside* it. NovaFabric records and verifies the assertion; it never generates
or embeds a SynthID watermark (proprietary — verify-only).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.compliance.export.art50_marking import (
    SYNTHID_PRESENCE_LABEL,
    MarkingMethod,
    attach_synthid_presence,
    build_dual_layer_receipt,
    build_marking_log,
    verify_synthid_assertion,
)
from novafabric.evidence.c2pa_exporter import C2PAManifestExporter


def _real_manifest(tmp_path: Path) -> dict:
    """A C2PA manifest built by the shipped ADR-0074 exporter."""
    capsule = tmp_path / "cap"
    capsule.mkdir()
    (capsule / "capsule.yaml").write_text(
        "run_id: run-1\nstatus: completed\nnovafabric_version: 0.86.0\n",
        encoding="utf-8",
    )
    return C2PAManifestExporter().build_manifest(capsule)


class TestMarkingLog:
    def test_logs_each_marking_event(self) -> None:
        log = build_marking_log(
            [
                {"content_id": "img-1", "methods": [MarkingMethod.c2pa_manifest]},
                {
                    "content_id": "img-2",
                    "methods": [MarkingMethod.c2pa_manifest, MarkingMethod.synthid_watermark],
                },
            ]
        )
        assert len(log.events) == 2
        assert log.events[0].marked_as == "ai_generated"
        assert MarkingMethod.synthid_watermark in log.events[1].methods

    def test_marking_event_requires_a_method(self) -> None:
        with pytest.raises(ValueError, match="method"):
            build_marking_log([{"content_id": "img-1", "methods": []}])


class TestSynthIdPresenceAssertion:
    def test_attach_injects_assertion_into_real_c2pa_manifest(self, tmp_path: Path) -> None:
        manifest = _real_manifest(tmp_path)
        out = attach_synthid_presence(
            manifest, present=True, detector="synthid-detector-v2", verified_at="2026-07-25T00:00:00Z"
        )
        # Non-mutating: original manifest is untouched.
        active = manifest["manifests"][manifest["active_manifest"]]
        assert all(a["label"] != SYNTHID_PRESENCE_LABEL for a in active["assertions"])
        # The assertion is carried INSIDE the C2PA active manifest.
        out_active = out["manifests"][out["active_manifest"]]
        labels = [a["label"] for a in out_active["assertions"]]
        assert SYNTHID_PRESENCE_LABEL in labels

    def test_verify_reads_the_assertion_back(self, tmp_path: Path) -> None:
        manifest = _real_manifest(tmp_path)
        out = attach_synthid_presence(
            manifest, present=True, detector="synthid-detector-v2", verified_at="2026-07-25T00:00:00Z"
        )
        assertion = verify_synthid_assertion(out)
        assert assertion is not None
        assert assertion.present is True
        assert assertion.detector == "synthid-detector-v2"

    def test_verify_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert verify_synthid_assertion(_real_manifest(tmp_path)) is None

    def test_assertion_records_verify_only_provenance(self, tmp_path: Path) -> None:
        out = attach_synthid_presence(
            _real_manifest(tmp_path),
            present=False,
            detector="synthid-detector-v2",
            verified_at="2026-07-25T00:00:00Z",
        )
        assertion = verify_synthid_assertion(out)
        assert assertion is not None
        assert assertion.present is False
        # NovaFabric never generates/embeds SynthID — the assertion says so.
        assert "never" in assertion.note.lower()


class TestDualLayerReceipt:
    def test_receipt_carries_both_layers(self, tmp_path: Path) -> None:
        manifest = _real_manifest(tmp_path)
        log = build_marking_log([{"content_id": "img-1", "methods": [MarkingMethod.c2pa_manifest]}])
        receipt = build_dual_layer_receipt(
            manifest,
            marking_log=log,
            synthid_present=True,
            detector="synthid-detector-v2",
            verified_at="2026-07-25T00:00:00Z",
        )
        # Layer 1: the C2PA manifest, now carrying the SynthID-presence assertion.
        assert verify_synthid_assertion(receipt.c2pa_manifest) is not None
        # Layer 2: the Art. 50 marking log.
        assert len(receipt.marking_log.events) == 1
