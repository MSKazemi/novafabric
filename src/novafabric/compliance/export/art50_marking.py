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
"""EU AI Act Art. 50 marking log + dual-layer provenance receipt (ADR-0107 §NF-094).

Art. 50 requires providers to disclose that content is AI-generated. NovaFabric logs
each marking/disclosure event and builds a **dual-layer** provenance receipt:

1. the **C2PA** manifest already emitted by the shipped ADR-0074 exporter
   (:class:`~novafabric.evidence.c2pa_exporter.C2PAManifestExporter`), and
2. a **SynthID-presence assertion** carried *inside* that C2PA manifest.

The load-bearing rule (ADR-0107 §NF-094): NovaFabric **records and verifies** the
SynthID-presence assertion — a claim, from an external detector, that a SynthID
watermark is or is not present — but **never generates or embeds a SynthID watermark
itself** (SynthID is proprietary; this is verify-only). :func:`attach_synthid_presence`
injects the assertion into the C2PA manifest without mutating the original, and
:func:`verify_synthid_assertion` reads it back — that read-back *is* the "NovaFabric
verifies the C2PA assertion" step.

Pure-code and offline: no infrastructure, no new dependencies. This ships NF-094;
NF-093/097 remain future design.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

#: C2PA assertion label under which the SynthID-presence claim is carried inside the
#: manifest. Namespaced to NovaFabric — it is our recorded/verified assertion, not a
#: Google SynthID artifact.
SYNTHID_PRESENCE_LABEL = "novafabric.synthid.presence"

_VERIFY_ONLY_NOTE = (
    "SynthID is proprietary; NovaFabric records and verifies this presence assertion "
    "but never generates or embeds a SynthID watermark."
)


class MarkingMethod(str, Enum):
    """How an AI-disclosure/marking was applied to a piece of content."""

    c2pa_manifest = "c2pa_manifest"
    synthid_watermark = "synthid_watermark"
    visible_disclosure = "visible_disclosure"


class MarkingEvent(BaseModel):
    """One Art. 50 AI-disclosure/marking event."""

    content_id: str
    marked_as: str = "ai_generated"
    methods: list[MarkingMethod]
    marked_at: str
    run_id: str | None = None


class Art50MarkingLog(BaseModel):
    """A log of Art. 50 marking events, ready to be sealed as evidence."""

    events: list[MarkingEvent] = Field(default_factory=list)
    generated_at: str


class SynthIdPresenceAssertion(BaseModel):
    """A recorded/verified SynthID-presence claim (verify-only)."""

    present: bool
    detector: str
    verified_at: str
    note: str = _VERIFY_ONLY_NOTE


class DualLayerReceipt(BaseModel):
    """The dual-layer provenance receipt: C2PA manifest + Art. 50 marking log."""

    c2pa_manifest: dict[str, Any]
    marking_log: Art50MarkingLog


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_marking_log(events: Sequence[Mapping[str, Any]]) -> Art50MarkingLog:
    """Build an Art. 50 marking log from disclosure events.

    Each event is ``{content_id, methods, marked_as?, marked_at?, run_id?}``. Every
    event must carry at least one :class:`MarkingMethod` — an Art. 50 marking with no
    method is not a disclosure, so it raises :class:`ValueError`.
    """
    built: list[MarkingEvent] = []
    for raw in events:
        methods = list(raw.get("methods") or [])
        if not methods:
            raise ValueError(
                f"marking event for '{raw.get('content_id')}' needs at least one method"
            )
        built.append(
            MarkingEvent(
                content_id=str(raw["content_id"]),
                marked_as=str(raw.get("marked_as", "ai_generated")),
                methods=[MarkingMethod(m) for m in methods],
                marked_at=str(raw.get("marked_at", _now())),
                run_id=raw.get("run_id"),
            )
        )
    return Art50MarkingLog(events=built, generated_at=_now())


def attach_synthid_presence(
    manifest: Mapping[str, Any],
    *,
    present: bool,
    detector: str,
    verified_at: str,
) -> dict[str, Any]:
    """Return a copy of *manifest* with a SynthID-presence assertion in its active manifest.

    The input manifest is **not** mutated. The assertion is appended to the assertions
    list of the manifest identified by ``active_manifest`` — carried *inside* the C2PA
    manifest, as ADR-0107 §NF-094 requires. Raises :class:`ValueError` if *manifest* has
    no resolvable active manifest.
    """
    out = copy.deepcopy(dict(manifest))
    active_id = out.get("active_manifest")
    manifests = out.get("manifests")
    if not active_id or not isinstance(manifests, dict) or active_id not in manifests:
        raise ValueError("manifest has no resolvable active_manifest to attach to")
    entry = manifests[active_id]
    assertions = entry.setdefault("assertions", [])
    assertions.append(
        {
            "label": SYNTHID_PRESENCE_LABEL,
            "data": {
                "present": present,
                "detector": detector,
                "verified_at": verified_at,
                "note": _VERIFY_ONLY_NOTE,
            },
        }
    )
    return out


def verify_synthid_assertion(
    manifest: Mapping[str, Any],
) -> SynthIdPresenceAssertion | None:
    """Read back the SynthID-presence assertion from a C2PA manifest, if present.

    This read-back is NovaFabric's verification of the C2PA assertion. Returns the
    parsed :class:`SynthIdPresenceAssertion`, or ``None`` if the active manifest carries
    no such assertion. Never raises on a well-formed C2PA manifest.
    """
    active_id = manifest.get("active_manifest")
    manifests = manifest.get("manifests")
    if not active_id or not isinstance(manifests, dict) or active_id not in manifests:
        return None
    for assertion in manifests[active_id].get("assertions", []):
        if assertion.get("label") == SYNTHID_PRESENCE_LABEL:
            data = assertion.get("data", {})
            return SynthIdPresenceAssertion(
                present=bool(data.get("present")),
                detector=str(data.get("detector", "")),
                verified_at=str(data.get("verified_at", "")),
                note=str(data.get("note", _VERIFY_ONLY_NOTE)),
            )
    return None


def build_dual_layer_receipt(
    manifest: Mapping[str, Any],
    *,
    marking_log: Art50MarkingLog,
    synthid_present: bool,
    detector: str,
    verified_at: str,
) -> DualLayerReceipt:
    """Build the dual-layer receipt: C2PA manifest (+ SynthID assertion) and marking log.

    The C2PA manifest is the layer-1 provenance; the SynthID-presence assertion is
    attached inside it (verify-only); the Art. 50 marking log is layer 2.
    """
    enriched = attach_synthid_presence(
        manifest,
        present=synthid_present,
        detector=detector,
        verified_at=verified_at,
    )
    return DualLayerReceipt(c2pa_manifest=enriched, marking_log=marking_log)
