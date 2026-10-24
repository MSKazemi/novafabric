"""The email patterns run in linear time on long lines (review compliance-privacy-07).

``[A-Za-z0-9._%+\\-]+@…`` with no start guard re-scans to the end of the line from
every position of a long ``@``-less run — a hex dump, a digest list, a minified
blob — so one 64 KiB line cost about a minute and a 1 MiB line hours, during capture
finalisation (the ``--pii-subject`` stage) with no time bound. A start guard keeps
matches to token starts, and RFC-sized caps (local part 64, domain 253, TLD 24)
bound each attempt; real addresses are detected exactly as before.
"""

from __future__ import annotations

import time

import pytest

from novafabric.compliance.pii.detector import _DEFAULT_PATTERNS
from novafabric.masking import examples

EMAIL = next(p["pattern"] for p in _DEFAULT_PATTERNS if p["entity_type"] == "EMAIL")
PATTERNS = {"detector": EMAIL, "masking-example": examples._EMAIL_RE}


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_a_one_mebibyte_token_line_is_scanned_in_well_under_a_second(name: str) -> None:
    line = "0123456789abcdef" * 65536  # 1 MiB, no "@"
    t0 = time.perf_counter()
    assert PATTERNS[name].findall(line) == []
    assert time.perf_counter() - t0 < 1.0


@pytest.mark.parametrize("name", sorted(PATTERNS))
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("contact alice@example.com now", ["alice@example.com"]),
        ("a.b+tag@sub.example.co.uk,x", ["a.b+tag@sub.example.co.uk"]),
        ("<bob_smith@mail.example.org>", ["bob_smith@mail.example.org"]),
        ("two: a@x.io; b@y.io", ["a@x.io", "b@y.io"]),
        ("not an address: foo@bar", []),
    ],
)
def test_real_addresses_are_still_found(name: str, text: str, expected: list[str]) -> None:
    assert PATTERNS[name].findall(text) == expected


@pytest.mark.parametrize("name", sorted(PATTERNS))
def test_an_address_after_a_long_token_is_still_found(name: str) -> None:
    line = "deadbeef" * 8192 + " carol@example.net"
    assert PATTERNS[name].findall(line) == ["carol@example.net"]
