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
"""Compliance document export: EU AI Act Annex IV, NIS2, GDPR RoPA, AI-SBOM, NIST AI RMF."""

from .aibom import AIBOMDocument, AIBOMExporter
from .annex_iv import AnnexIVExporter
from .gdpr_ropa import GDPRRoPAExporter, RoPADocument, RoPAEntry
from .models import (
    AnnexIVDocument,
    AnnexIVElement,
    CompletenessStatus,
    CompletenessSummaryEntry,
    NIS2IncidentReport,
    PopulationMethod,
)
from .nis2 import NIS2Exporter
from .nist_rmf import NISTAIRMFReporter, NISTRMFReport, RMFMetric
from .provenance import (
    EvidenceSource,
    EvidenceSourceRef,
    MissingReperformableRefError,
    ProvenanceError,
    UnmarkedFieldGroupError,
    build_capsule_ref,
    mark,
    source_for_status,
    validate_marked,
)
from .renderer import DocumentRenderer

__all__ = [
    "AIBOMDocument",
    "AIBOMExporter",
    "AnnexIVDocument",
    "AnnexIVElement",
    "AnnexIVExporter",
    "CompletenessStatus",
    "CompletenessSummaryEntry",
    "DocumentRenderer",
    "EvidenceSource",
    "EvidenceSourceRef",
    "GDPRRoPAExporter",
    "MissingReperformableRefError",
    "ProvenanceError",
    "UnmarkedFieldGroupError",
    "build_capsule_ref",
    "mark",
    "source_for_status",
    "validate_marked",
    "NISTAIRMFReporter",
    "NISTRMFReport",
    "NIS2Exporter",
    "NIS2IncidentReport",
    "PopulationMethod",
    "RMFMetric",
    "RoPADocument",
    "RoPAEntry",
]
