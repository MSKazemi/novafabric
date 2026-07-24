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

"""Source-integrity pin — ADR-0153 D3 / P1 (NF-215).

Pins the *exact retrieved version* of a source so a later content change is
detectable: PoisonedRAG showed five documents injected into a 2.6M-document
corpus steer a frontier model 97% of the time, so "which version grounded this
answer" is forensic evidence.

Four invariants from ADR-0153 shape every choice in this module:

- **I-1 Record-only / no corpus management.** A pin mismatch is *evidence*.
  Nothing here fetches, re-fetches, crawls, repairs, or rebuilds a source —
  verification is a pure function of the pin and content the caller already
  holds. The module imports no network client, by design.
- **I-2 No payloads.** A retrieved body is represented only by its ``sha256:``
  digest. Bodies stay behind the shipped ``RetrievedDocument.content`` opt-in
  and its redaction pass (ADR-0021 §4).
- **I-3 Fail-open.** An unpinned document produces no verdict and no exception.
- **I-4 Not adjudicated.** A matching pin says the bytes are the same bytes.
  It does not say the source was authoritative, lawful, or true.

**Absent is not false.** A document with no ``content_hash`` is *unknown*, not
*unmodified*. ``pin_status`` reports that third state explicitly, and
``verify_document_pin`` refuses to return True for it — an unpinned document
must never pass a verifier that exists to check pins (the same stance
``trust/capsule_flags.py`` takes when it leaves an unverifiable axis absent
rather than reporting a failure).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from typing import Literal

from novafabric.capture.events import RetrievedDocument

#: ``match`` — the pin re-computes over the supplied content.
#: ``mismatch`` — the content changed since retrieval (tamper/drift evidence).
#: ``unpinned`` — the document carries no pin; the question is unanswerable.
PinStatus = Literal["match", "mismatch", "unpinned"]


def digest_document(content: str | bytes) -> str:
    """Return the ``sha256:`` digest of a retrieved document body.

    Emitted in the same ``sha256:<hex>`` form the rest of the capsule uses, so
    a verifier does not have to know which subsystem wrote the digest.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def pin_status(document: RetrievedDocument, content: str | bytes) -> PinStatus:
    """Compare a document's pin against content the caller already holds.

    Returns ``unpinned`` — never ``match`` — when the document carries no
    ``content_hash``. Collapsing "no pin" into either verdict would fabricate
    a finding: a green "match" for a document nobody pinned, or a "mismatch"
    accusing an unpinned source of tampering.
    """
    if not document.content_hash:
        return "unpinned"
    return "match" if document.content_hash == digest_document(content) else "mismatch"


def verify_document_pin(document: RetrievedDocument, content: str | bytes) -> bool:
    """Re-verify a document's integrity pin offline.

    Returns False for an unpinned document. An unpinned document is not
    "trivially intact" — it is precisely the gap ADR-0153 wants surfaced, and
    returning True would let a document with no binding pass a verifier whose
    only job is to check bindings. Callers that need to tell "unpinned" from
    "changed" must use :func:`pin_status`.
    """
    return pin_status(document, content) == "match"


def pinned_documents(
    documents: Iterable[RetrievedDocument],
) -> list[RetrievedDocument]:
    """Return only the documents that carry an integrity pin.

    Lets a caller report coverage ("3 of 10 retrieved documents are pinned")
    instead of silently verifying the pinned subset and calling the whole
    retrieval verified.
    """
    return [d for d in documents if d.content_hash]
