"""ADR-0162 P3 hardening of the shared I-2 boundary (``embodied/_boundary.py``).

- a ``sha256:`` ref with a trailing newline is not the digest it imitates
  (``fullmatch``, never ``$``);
- payload-named keys are caught after normalisation (case, camelCase,
  separators, NFKC) and by ``raw_`` prefix / payload-noun suffix markers,
  while the reference-shaped names the facet legitimately uses survive;
- MIME-wrapped base64 and a whitespace-prefixed ``data:`` URI are caught;
- P1/P2 free strings are length-capped.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from novafabric.embodied import (
    InvalidReferenceError,
    OddExcursion,
    RawPayloadRejectedError,
    SensorStream,
    reject_raw_payloads,
)

DIGEST = f"sha256:{'a' * 64}"


def _stream(**kw: Any) -> SensorStream:
    base: dict[str, Any] = {
        "sensor_id": "front-cam-0",
        "modality": "camera",
        "frame_count": 1,
        "stream_digest": DIGEST,
        "clock_domain": "ptp-0",
    }
    base.update(kw)
    return SensorStream(**base)


@pytest.mark.parametrize("bad", [DIGEST + "\n", DIGEST + "\r\n", " " + DIGEST])
def test_digest_with_whitespace_is_refused(bad: str) -> None:
    with pytest.raises(InvalidReferenceError):
        _stream(stream_digest=bad)


@pytest.mark.parametrize(
    "key",
    [
        "Frames",
        " frames ",
        "rawBytes",
        "image-data",
        "raw_scan",
        "lidarFrames",
        "cabin.audio",
        "ｆｒａｍｅｓ",  # full-width, folds under NFKC
        "front_point_cloud",
    ],
)
def test_payload_named_keys_are_caught_after_normalisation(key: str) -> None:
    with pytest.raises(RawPayloadRejectedError, match="payload-named"):
        reject_raw_payloads({key: "x"})


@pytest.mark.parametrize(
    "key",
    [
        "frame_count",
        "audio_stream_digest",
        "point_cloud_ref",
        "sensor_id",
        "metadata",
        "operator_ref",
    ],
)
def test_reference_shaped_keys_survive(key: str) -> None:
    reject_raw_payloads({key: "x"})


def test_mime_wrapped_base64_is_caught() -> None:
    wrapped = "\n".join(["QUJD" * 19] * 5)  # 76-column lines, > 256 chars
    with pytest.raises(RawPayloadRejectedError, match="base64"):
        reject_raw_payloads({"note": wrapped})


def test_long_prose_is_left_alone() -> None:
    reject_raw_payloads({"note": "the quick brown fox " * 30})


def test_whitespace_prefixed_data_uri_is_caught() -> None:
    with pytest.raises(RawPayloadRejectedError, match="data: URI"):
        reject_raw_payloads({"note": "  data:image/png;base64,AAAA"})


@pytest.mark.parametrize("field", ["sensor_id", "clock_domain"])
def test_p1_free_strings_are_capped(field: str) -> None:
    with pytest.raises(ValidationError):
        _stream(**{field: "x " * 200})


def test_p2_excursion_strings_are_capped() -> None:
    with pytest.raises(ValidationError):
        OddExcursion(condition="fog", observed="y " * 200, ts="2026-07-13T10:02:11Z")
