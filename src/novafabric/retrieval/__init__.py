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

"""Retrieval-source authority & knowledge provenance (ADR-0153, experimental).

Record-only: NovaFabric records provenance *about* sources an external
retriever or the agent itself fetched. It never fetches, re-fetches, crawls,
ranks, hosts, or rebuilds a corpus, and it never adjudicates a source's
authority, license, or truth.
"""

from novafabric.retrieval.fetch_provenance import (
    FetchProvenance,
    FetchProvenanceFacet,
    ForbiddenFetchFieldError,
    InvalidRedirectChainError,
    RedirectHop,
    SecretMaterialError,
    attach_facet,
    build_facet,
    digest_body,
    fingerprint_leaf_certificate,
    record_fetch,
    verify_body_binding,
)
from novafabric.retrieval.source_pin import (
    PinStatus,
    digest_document,
    pin_status,
    pinned_documents,
    verify_document_pin,
)

__all__ = [
    "FetchProvenance",
    "FetchProvenanceFacet",
    "ForbiddenFetchFieldError",
    "InvalidRedirectChainError",
    "PinStatus",
    "RedirectHop",
    "SecretMaterialError",
    "attach_facet",
    "build_facet",
    "digest_body",
    "digest_document",
    "fingerprint_leaf_certificate",
    "pin_status",
    "pinned_documents",
    "record_fetch",
    "verify_body_binding",
    "verify_document_pin",
]
