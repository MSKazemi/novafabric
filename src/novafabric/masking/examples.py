"""Reference maskers (ADR-0135) — stdlib/regex only, offline, Tier-A.

``EmailMasker`` is the canonical example of the masker plugin contract.
It ships registered under the ``novafabric.maskers`` entry-point group as
``novafabric-email`` and is also reachable by dotted import path::

    # .novafabric/masking.yaml
    masking:
      enabled: true
      maskers:
        - id: novafabric-email                          # entry-point name
        # or:
        - id: novafabric.masking.examples:EmailMasker   # dotted import path
          config:
            replacement: "[MASKED:email]"

Use it as the template for org-specific maskers: declare ``masker_id``,
``masker_version``, ``pattern_ids``; keep ``mask()`` pure, deterministic,
and offline; return ``UNCHANGED`` to decline.
"""
from __future__ import annotations

import re

from novafabric.masking._models import UNCHANGED, MaskContext, MaskField, _Unchanged

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_DEFAULT_REPLACEMENT = "[MASKED:email]"


class EmailMasker:
    """Mask RFC-5322-shaped email addresses inside a string value.

    Pure and deterministic: the same value always yields the same output.
    The replacement marker is configurable via ``config.replacement`` in
    ``masking.yaml`` (surfaced as ``context.masker_config``).
    """

    masker_id = "novafabric-email"
    masker_version = "1"
    pattern_ids = ("email-address",)

    def mask(
        self, field: MaskField, value: str, context: MaskContext
    ) -> str | _Unchanged:
        replacement = str(
            context.masker_config.get("replacement", _DEFAULT_REPLACEMENT)
        )
        masked = _EMAIL_RE.sub(replacement, value)
        if masked == value:
            return UNCHANGED
        return masked
