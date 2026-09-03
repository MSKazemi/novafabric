"""The assurance-honesty line (ADR-0147 I-4).

Defined once because two CLI groups must print the *same* words. ADR-0147 makes
it a MUST on every ``drift``/``assure`` output, and the reason is behavioural
rather than legal: the detectors render verdict-shaped output — ``DRIFTED`` in
red, ``silent-failure`` in red — and a detector that looks like a gate will be
treated as one. This line is what separates "this exceeded the threshold you
declared" from "NovaFabric judges your model to be broken".
"""

from __future__ import annotations

HONESTY_LINE = (
    "NovaFabric records that drift occurred and its probable cause. It does not "
    "remediate, retrain, roll back, or assert the drift is acceptable."
)

__all__ = ["HONESTY_LINE"]
