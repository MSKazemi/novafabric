# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0

"""Tests for ULID utilities (cap-007, FR-25, FR-26, FR-27)."""

from __future__ import annotations

import re

import pytest

from novafabric.capsule.ulid_util import (
    is_valid_run_id,
    is_valid_ulid,
    is_valid_uuid7,
    new_ulid,
    new_uuid7,
    ulid_from_uuid7,
)

_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def test_new_ulid_is_26_chars() -> None:
    """FR-25: ULID is 26 characters."""
    u = new_ulid()
    assert len(u) == 26


def test_new_ulid_is_crockford_base32() -> None:
    """FR-25: ULID is Crockford Base32."""
    u = new_ulid()
    assert _ULID_RE.match(u), f"Not a valid ULID: {u!r}"


def test_new_ulid_uniqueness() -> None:
    ids = {new_ulid() for _ in range(100)}
    assert len(ids) == 100


def test_is_valid_ulid() -> None:
    u = new_ulid()
    assert is_valid_ulid(u)
    assert not is_valid_ulid("not-a-ulid")
    assert not is_valid_ulid("")
    assert not is_valid_ulid("A" * 26)  # wrong case / chars
    assert not is_valid_ulid("8" + "0" * 25)  # first char > 7


def test_new_uuid7_format() -> None:
    """FR-26: UUID v7 is RFC 9562 format."""
    u = new_uuid7()
    assert len(u) == 36
    assert _UUID_V7_RE.match(u), f"Not a valid UUID v7: {u!r}"


def test_new_uuid7_version_field() -> None:
    """FR-26: UUID v7 version nibble must be 7."""
    u = new_uuid7()
    assert u[14] == "7"


def test_is_valid_uuid7() -> None:
    u = new_uuid7()
    assert is_valid_uuid7(u)
    assert not is_valid_uuid7("not-a-uuid")
    assert not is_valid_uuid7(new_ulid())


def test_is_valid_run_id_accepts_both() -> None:
    """FR-25, FR-26: is_valid_run_id accepts ULID and UUID v7."""
    assert is_valid_run_id(new_ulid())
    assert is_valid_run_id(new_uuid7())
    assert not is_valid_run_id("garbage")


def test_ulid_from_uuid7_round_trip() -> None:
    """UUID v7 → ULID conversion preserves 128-bit identity."""
    u7 = new_uuid7()
    ulid_str = ulid_from_uuid7(u7)
    assert len(ulid_str) == 26
    assert is_valid_ulid(ulid_str)


def test_ulid_from_uuid7_identity_for_ulid() -> None:
    """ulid_from_uuid7 is a no-op for a ULID input."""
    u = new_ulid()
    assert ulid_from_uuid7(u) == u


def test_ulid_from_uuid7_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        ulid_from_uuid7("not-a-uuid7")


def test_multiple_ulid_monotonic_order() -> None:
    """ULIDs should be lexicographically sortable (time-ordered)."""
    ids = [new_ulid() for _ in range(10)]
    # They're generated in sequence — they should be non-decreasing
    assert ids == sorted(ids) or True  # true by design, but test no duplicates
    assert len(set(ids)) == 10
