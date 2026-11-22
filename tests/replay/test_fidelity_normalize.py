"""The fidelity verdict's declared volatile-token rule set ``volatile-ids/1``.

One rule -- RFC 4122 UUIDs in canonical form -- because that is the only one the
evidence required (AWS ``log_mcp``: LangChain ``lc_<uuid4>`` ids). Everything the
rule does not name is left as it is.
"""

from __future__ import annotations

import uuid

import pytest

from novafabric.replay._normalize import NORMALIZER_ID, normalize_volatile


def test_the_rule_set_is_named_and_versioned() -> None:
    assert NORMALIZER_ID == "volatile-ids/1"


@pytest.mark.parametrize("make", [uuid.uuid1, uuid.uuid4, lambda: uuid.uuid5(uuid.NAMESPACE_DNS, "x")])
def test_every_rfc4122_version_in_canonical_form_is_masked(make: object) -> None:
    value = str(make())  # type: ignore[operator]
    assert normalize_volatile(f"lc_{value}".encode()) == b"lc_<uuid:1>"
    assert normalize_volatile(value.upper().encode()) == b"<uuid:1>"


def test_ids_are_numbered_by_first_appearance_and_case_folded() -> None:
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    text = f"{a} {b} {a.upper()}".encode()
    assert normalize_volatile(text) == b"<uuid:1> <uuid:2> <uuid:1>"


@pytest.mark.parametrize("text", [
    b"00000000-0000-0000-0000-000000000000",  # nil UUID: version 0
    b"3f2b8c1e-9d4a-6f6b-8a2c-1e5d7f9b0c3a",  # version 6 (RFC 9562, not 4122)
    b"3f2b8c1e-9d4a-4f6b-ca2c-1e5d7f9b0c3a",  # variant 110x (Microsoft), not 10xx
    b"3f2b8c1e9d4a4f6b8a2c1e5d7f9b0c3a",  # uuid.hex: no hyphens
    b"a3f2b8c1e-9d4a-4f6b-8a2c-1e5d7f9b0c3a",  # inside a longer hex run
    b"3f2b8c1e-9d4a-4f6b-8a2c-1e5d7f9b0c3ab",
    b"2026-11-20T10:00:00.123456Z",
    b"sha256:" + b"ab" * 32,
])
def test_nothing_outside_the_rule_is_touched(text: bytes) -> None:
    assert normalize_volatile(text) == text


def test_bytes_that_are_not_utf8_pass_through() -> None:
    value = str(uuid.uuid4()).encode()
    assert normalize_volatile(b"\xff\xfe" + value + b"\x00") == b"\xff\xfe<uuid:1>\x00"
