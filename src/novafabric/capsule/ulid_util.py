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

"""ULID generation and UUID v7 acceptance utilities (cap-007, FR-25, FR-26, FR-27).

Uses python-ulid (Apache-2.0) for ULID generation.
UUID v7 (RFC 9562) is accepted as a drop-in alternative; storage treats both
as opaque 128-bit values.

CLI backend: `nova new-run-id` prints a fresh ULID.
"""

from __future__ import annotations

import re
import uuid

from ulid import ULID

_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_UUID_V7_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def new_ulid() -> str:
    """Generate a new ULID — 26-character Crockford Base32 (FR-25, FR-27)."""
    return str(ULID())


def new_uuid7() -> str:
    """Generate a new UUID v7 (RFC 9562, 36-char).

    NOTE: Python 3.12 stdlib does not have uuid.uuid7(); we implement it per RFC 9562:
      bits 0–47: unix_ts_ms (big-endian millisecond timestamp)
      bits 48–51: version = 0x7
      bits 52–63: rand_a (12 random bits)
      bits 64–65: variant = 0b10
      bits 66–127: rand_b (62 random bits)
    """
    import os
    import time

    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # 48 bits

    rand_a = int.from_bytes(os.urandom(2), "big") & 0xFFF          # 12 bits
    rand_b = int.from_bytes(os.urandom(8), "big") & 0x3FFFFFFFFFFFFFFF  # 62 bits

    # Pack into 128-bit integer per RFC 9562 layout:
    # bits 127–80: unix_ts_ms (48 bits)
    # bits 79–76:  version = 0x7 (4 bits)
    # bits 75–64:  rand_a (12 bits)
    # bits 63–62:  variant = 0b10 (2 bits)
    # bits 61–0:   rand_b (62 bits)
    int_val = (
        (ts_ms << 80)
        | (0x7 << 76)
        | (rand_a << 64)
        | (0b10 << 62)
        | rand_b
    )
    u = uuid.UUID(int=int_val)
    return str(u)


def is_valid_ulid(value: str) -> bool:
    """Return True iff value is a valid 26-char Crockford Base32 ULID (FR-25)."""
    return bool(_ULID_RE.match(value))


def is_valid_uuid7(value: str) -> bool:
    """Return True iff value is a valid 36-char RFC 9562 UUID v7 (FR-26)."""
    return bool(_UUID_V7_RE.match(value))


def is_valid_run_id(value: str) -> bool:
    """Return True iff value is a valid ULID or UUID v7 (FR-25, FR-26)."""
    return is_valid_ulid(value) or is_valid_uuid7(value)


def ulid_from_uuid7(uuid7_str: str) -> str:
    """Convert a UUID v7 string to a ULID string (for storage normalisation).

    Both represent a 128-bit value; this extracts the bytes and re-encodes
    as Crockford Base32. Returns the original if already a ULID.
    """
    if is_valid_ulid(uuid7_str):
        return uuid7_str
    if not is_valid_uuid7(uuid7_str):
        raise ValueError(f"Not a valid UUID v7: {uuid7_str!r}")
    u = uuid.UUID(uuid7_str)
    return str(ULID.from_uuid(u))
