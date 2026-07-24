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

"""Primitives shared by every ``facets.safety`` object (ADR-0145).

Extracted when P2 landed, for one structural reason: the facet lives in
``decisions`` and must hold the P2 attempt objects, so ``decisions`` has to
import ``attempts`` — which leaves nowhere below both for the digest function
and the detector block they share. Duplicating either would be worse than the
extra module: two hash helpers in one facet is how a capsule ends up with two
incompatible digest constructions, and two detector models is how attribution
quietly diverges between halves of the same evidence.

Nothing here is public API. ``decisions`` and ``attempts`` re-export what
callers need, so the shipped import paths are unchanged.
"""

from __future__ import annotations

import hashlib
import re

from pydantic import BaseModel, ConfigDict

#: The canonical digest form used everywhere in the capsule.
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def digest_bytes(content: str | bytes) -> str:
    """Return ``sha256:<hex>`` over ``content``.

    The single hash construction for the whole safety facet. A verifier must
    never have to know which half of the facet wrote a digest.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


class DetectorProvenance(BaseModel):
    """Who produced a verdict (I-4: attribution, not endorsement)."""

    model_config = ConfigDict(extra="allow")

    name: str
    version: str | None = None
    vendor: str | None = None
