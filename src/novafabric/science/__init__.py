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

"""Scientific reproducibility & research-integrity evidence (ADR-0164, experimental).

Record-only: NovaFabric records the provenance of agentic science. It never runs
experiments, controls instruments, drives a self-driving lab, generates
hypotheses, or adjudicates scientific validity.
"""

from novafabric.science.provenance import (
    CyclicLineageError,
    DagTooLargeError,
    DagVerification,
    DuplicateNodeError,
    InvalidNodeDigestError,
    PayloadCaptureError,
    ScienceNode,
    ScienceProvenanceError,
    ScienceProvenanceFacet,
    UnresolvedParentError,
    attach_facet,
    build_dag,
    build_facet,
    digest_node,
    facet_from_capsule,
    verify_dag,
    verify_node_digest,
)

__all__ = [
    "CyclicLineageError",
    "DagTooLargeError",
    "DagVerification",
    "DuplicateNodeError",
    "InvalidNodeDigestError",
    "PayloadCaptureError",
    "ScienceNode",
    "ScienceProvenanceError",
    "ScienceProvenanceFacet",
    "UnresolvedParentError",
    "attach_facet",
    "build_dag",
    "build_facet",
    "digest_node",
    "facet_from_capsule",
    "verify_dag",
    "verify_node_digest",
]
