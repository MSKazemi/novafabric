"""Content checks shared by the public disclosures: model internals and raw identifiers.

The checks were substring matches over a token list, which rejected legitimate plain-language
text ("body weight"; "homelessness" contains ``ssn``) while a literal ``123-45-6789`` passed
(review compliance-privacy-23). They now match whole words, refine ``weight`` to the
model-parameter senses, and detect identifier *values* — a dashed US SSN and a payment-card
number that passes the Luhn check.
"""

from __future__ import annotations

import re

#: Whole-word / compound shapes of model internals. ``weight`` alone is a normal word
#: ("body weight"); the plural and the parameter senses are not.
_MODEL_INTERNALS = re.compile(
    r"\bweights\b"
    r"|\b(?:model|network|layer|neural|parameter)s?[ _-]weights?\b"
    r"|\bweights?[ _-](?:matrix|matrices|vector|tensor|decay|sharing)\b"
    r"|\bgradients?\b|\blogits?\b|\bactivations?\b|\bembeddings?\b"
    r"|\bhyperparameters?\b|\bneurons?\b|\bsoftmax\b|\bhidden_state\b"
    r"|\battention_heads?\b|\blayer_\w+|\bcheckpoint_step\b",
    re.IGNORECASE,
)

#: Names of raw sensitive identifiers (whole words; underscore or space between the parts).
_IDENTIFIER_NAMES = re.compile(
    r"\bssn\b|\bsocial[ _]security\b|\bpassport[ _]number\b|\bcredit[ _]card\b"
    r"|\bnational[ _]id\b",
    re.IGNORECASE,
)

_SSN_VALUE = re.compile(r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])")
_CARD_CANDIDATE = re.compile(r"(?<![\d])(?:\d[ -]?){13,19}(?![\d])")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        n = int(char)
        if index % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def mentions_model_internals(text: str) -> bool:
    return _MODEL_INTERNALS.search(text) is not None


def mentions_raw_identifier(text: str) -> bool:
    """True for an identifier name (``SSN``, ``credit card`` …) or an identifier value."""
    if _IDENTIFIER_NAMES.search(text) or _SSN_VALUE.search(text):
        return True
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"[ -]", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            return True
    return False
