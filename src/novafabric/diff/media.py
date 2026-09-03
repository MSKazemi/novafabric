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

"""Multi-modal media diff by exact and perceptual hash (ADR-0148 D3 / NF-170).

Compares two runs' media parts. The **exact** comparison is the default and needs nothing beyond
the ``content_hash`` each part already records; the **perceptual** comparison is opt-in, and
answers the question exact hashing cannot: *is this the same image, re-encoded?*

**⚠ `changed` needs an identity that content cannot supply.** With only content hashes you can
report ``identical`` / ``added`` / ``removed`` and nothing else — a changed part hashes
differently, so it looks like a removal plus an addition. Pairing parts therefore uses their
**position** (the *n*th media part of the *n*th model call), which is an *assumption* about the
two runs rather than a fact about them. The basis is named in the result (``pairing``) instead of
being left implicit, because a wrong pairing produces confident nonsense.

**⚠ No image decoder is a dependency of this package.** The pHash is computed here in stdlib
Python over already-decoded greyscale pixels, and the decoder is *injected*. A caller with Pillow
installed can pass :func:`pillow_decoder`; without one, perceptual comparison is **refused** rather
than quietly downgraded to exact-only — "we could not look" must not render as "we looked and
found nothing".

**⚠ A pair that could not be compared perceptually stays `changed`**, with a stated reason. It is
never promoted to ``near-duplicate`` on missing evidence, and never presented as though the
perceptual check had run and cleared it.

It **reports; it does not gate**: the classifications are facts about two runs, not a verdict.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from novafabric.capture.media import iter_media_parts

SCHEMA_VERSION = "0.1.0"

#: How parts are matched across the two runs. Recorded, not assumed silently.
PAIRING = "positional"

#: pHash working size. 32×32 greyscale, DCT-reduced to the top-left 8×8 = 64 bits.
_PHASH_SIZE = 32
_PHASH_LOW = 8

#: Coefficients within this fraction of the largest magnitude are treated as exactly zero. Without
#: it, a sparse DCT leaves the median sitting in floating-point dust and the hash stops being a
#: function of the image.
_PHASH_EPSILON = 1e-9

#: Minimum non-zero low-frequency coefficients for a perceptual comparison to mean anything.
#: An image whose energy is all high-frequency (a fine checkerboard) or all DC (a flat fill) has a
#: near-empty low block, and *every* such image hashes alike — so they would all compare as
#: near-duplicates of each other. Below this floor the comparison is reported unavailable rather
#: than answered.
MIN_LOW_FREQUENCY_COMPONENTS = 4

#: Default Hamming distance within which two images are called near-duplicates. A default is
#: supplied because the *scale* is fixed (64 bits), unlike the drift thresholds where the scale is
#: the caller's data — but it is reported with every pair so the call can be disagreed with.
DEFAULT_HAMMING_THRESHOLD = 10

MediaVerdict = Literal["identical", "near-duplicate", "changed", "added", "removed"]

#: A decoder turns raw bytes into ``(width, height, greyscale_pixels)``. Injected so this package
#: depends on no image library.
Decoder = Callable[[bytes], "tuple[int, int, Sequence[float]]"]


class MediaDiffError(ValueError):
    """Raised when a media diff cannot be produced honestly."""


class MediaPairDiff(BaseModel):
    """One paired media part across the two runs."""

    model_config = ConfigDict(frozen=True)

    index: int
    verdict: MediaVerdict
    media_type: str | None = None
    hash_a: str | None = None
    hash_b: str | None = None
    #: Hamming distance between the two pHashes; present only when both were computed.
    hamming: int | None = None
    threshold: int | None = None
    #: Why the perceptual comparison did not happen, when it was asked for and did not.
    perceptual_unavailable: str | None = None


class MediaDiff(BaseModel):
    """The media comparison of two runs.

    ``pairing`` states how parts were matched, because the matching is an assumption and a wrong
    one produces confident nonsense.
    """

    model_config = ConfigDict(frozen=True)

    pairing: str = PAIRING
    perceptual: bool = False
    parts_a: int = 0
    parts_b: int = 0
    pairs: list[MediaPairDiff] = Field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    # Intentionally NO regressed/passed/verdict field — it reports, it does not gate.

    @property
    def counts(self) -> dict[str, int]:
        """Verdict → number of pairs."""
        out: dict[str, int] = {}
        for pair in self.pairs:
            out[pair.verdict] = out.get(pair.verdict, 0) + 1
        return out


# ── Perceptual hash ───────────────────────────────────────────────────────


def _dct_1d(vector: Sequence[float]) -> list[float]:
    """Type-II DCT, direct form. n=32 here, so the O(n²) form is not worth optimising."""
    n = len(vector)
    return [
        sum(vector[x] * math.cos(math.pi * (x + 0.5) * u / n) for x in range(n))
        for u in range(n)
    ]


def phash(pixels: Sequence[float], size: int = _PHASH_SIZE) -> int:
    """Compute a 64-bit DCT perceptual hash over ``size × size`` greyscale *pixels*.

    Row-major. Returns an ``int`` whose 64 low bits are the hash: each of the top-left 8×8 DCT
    coefficients (excluding the DC term from the median) contributes one bit, set when the
    coefficient is above the median. That construction is what makes it survive re-encoding — the
    low-frequency structure of an image is preserved by lossy compression while its bytes are not.

    A featureless image (one uniform colour) has no structure to hash and yields ``0``; two
    different flat images are therefore perceptually equal, which is the honest answer — the exact
    ``content_hash`` is what distinguishes them.

    Raises:
        MediaDiffError: if the pixel count is not ``size * size``.
    """
    if len(pixels) != size * size:
        raise MediaDiffError(
            f"expected {size * size} greyscale pixels for a {size}x{size} pHash, got {len(pixels)}"
        )
    rows = [
        _dct_1d(list(pixels[r * size : (r + 1) * size])) for r in range(size)
    ]
    columns = [_dct_1d([rows[r][c] for r in range(size)]) for c in range(size)]
    # columns[c][r] is the coefficient at (row r, col c) after both passes.
    low = [columns[c][r] for r in range(_PHASH_LOW) for c in range(_PHASH_LOW)]

    # ⚠ Snap numerical dust to exactly zero before comparing. A smooth or flat image — a
    # screenshot, a chart, anything with large uniform regions — has a sparse DCT whose "zero"
    # coefficients are actually ±1e-14 of rounding error. Left alone, the median lands *inside*
    # that dust and roughly half the bits get decided by floating-point noise: measured here, a
    # brightness-only change (which perturbs nothing but the DC term) flipped 12 of 64 bits.
    magnitude = max((abs(v) for v in low), default=0.0)
    epsilon = magnitude * _PHASH_EPSILON
    low = [0.0 if abs(v) <= epsilon else v for v in low]

    # The DC term encodes overall brightness, not structure; excluding it from the median keeps a
    # uniformly brighter copy of the same image near its original.
    without_dc = low[1:]
    median = sorted(without_dc)[len(without_dc) // 2]
    bits = 0
    for i, value in enumerate(low):
        if value > median:
            bits |= 1 << i
    return bits


def low_frequency_components(pixels: Sequence[float], size: int = _PHASH_SIZE) -> int:
    """How many low-frequency DCT coefficients are non-zero — the hash's information content.

    Used to refuse a perceptual comparison that would be meaningless. See
    :data:`MIN_LOW_FREQUENCY_COMPONENTS`.
    """
    rows = [_dct_1d(list(pixels[r * size : (r + 1) * size])) for r in range(size)]
    columns = [_dct_1d([rows[r][c] for r in range(size)]) for c in range(size)]
    low = [columns[c][r] for r in range(_PHASH_LOW) for c in range(_PHASH_LOW)]
    magnitude = max((abs(v) for v in low), default=0.0)
    epsilon = magnitude * _PHASH_EPSILON
    # The DC term is brightness, not structure, so it does not count toward comparability.
    return sum(1 for v in low[1:] if abs(v) > epsilon)


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two hashes."""
    return bin(a ^ b).count("1")


def pillow_decoder(raw: bytes) -> tuple[int, int, list[float]]:
    """A :data:`Decoder` backed by Pillow, if the caller has it installed.

    Pillow is **not** a declared dependency. Its licence (MIT-CMU) is not enumerated in ADR-0024's
    Tier A list, so declaring it is an owner decision rather than one made in passing — and it is
    currently present only *transitively* (WeasyPrint pulls it in), which is exactly the kind of
    availability that disappears without warning when an unrelated extra changes. Hence the
    injected-decoder design: this function is a convenience for a caller who already has Pillow,
    not a dependency of this module.

    Raises:
        MediaDiffError: if Pillow is not importable, or the bytes do not decode.
    """
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise MediaDiffError(
            "perceptual comparison needs an image decoder and Pillow is not installed; "
            "exact comparison is unaffected"
        ) from exc
    import io  # noqa: PLC0415

    try:
        image = Image.open(io.BytesIO(raw)).convert("L").resize((_PHASH_SIZE, _PHASH_SIZE))
    except Exception as exc:  # noqa: BLE001 - any decode failure is the same answer here
        raise MediaDiffError(f"could not decode media bytes: {exc}") from exc
    # tobytes() over getdata(): one greyscale byte per pixel, and a typed return.
    return _PHASH_SIZE, _PHASH_SIZE, [float(b) for b in image.tobytes()]


# ── The diff ──────────────────────────────────────────────────────────────


def _parts(capsule_dir: Path) -> list[dict[str, Any]]:
    return [media for _, media in iter_media_parts(capsule_dir)]


def _blob_bytes(capsule_dir: Path, part: dict[str, Any]) -> bytes | None:
    blob_ref = part.get("blob_ref")
    if not isinstance(blob_ref, str) or not blob_ref:
        return None
    path = capsule_dir / blob_ref
    if not path.is_file():
        return None
    return path.read_bytes()


def _perceptual_pair(
    capsule_a: Path,
    capsule_b: Path,
    part_a: dict[str, Any],
    part_b: dict[str, Any],
    decoder: Decoder,
    threshold: int,
) -> tuple[int | None, str | None]:
    """Return ``(hamming_distance, unavailable_reason)`` — exactly one is not None."""
    raw_a, raw_b = _blob_bytes(capsule_a, part_a), _blob_bytes(capsule_b, part_b)
    if raw_a is None or raw_b is None:
        missing = "A" if raw_a is None else "B"
        return None, f"no stored bytes for side {missing} (blob_ref absent or file missing)"
    try:
        _, _, pixels_a = decoder(raw_a)
        _, _, pixels_b = decoder(raw_b)
        for label, pixels in (("A", pixels_a), ("B", pixels_b)):
            if low_frequency_components(pixels) < MIN_LOW_FREQUENCY_COMPONENTS:
                return None, (
                    f"side {label} has too little low-frequency structure to compare "
                    "perceptually (a flat or purely high-frequency image hashes like every "
                    "other one, so any answer would be a coincidence)"
                )
        return hamming(phash(pixels_a), phash(pixels_b)), None
    except MediaDiffError as exc:
        return None, str(exc)


def diff_media(
    capsule_a: str | Path,
    capsule_b: str | Path,
    *,
    perceptual: bool = False,
    decoder: Decoder | None = None,
    threshold: int = DEFAULT_HAMMING_THRESHOLD,
) -> MediaDiff:
    """Compare the media parts of two capsules.

    Exact comparison is always performed. When *perceptual* is set, each ``changed`` pair is
    additionally compared by pHash and promoted to ``near-duplicate`` if within *threshold* — and
    the distance is reported either way, so the classification can be disagreed with.

    Raises:
        MediaDiffError: if a capsule directory is missing, or *perceptual* is requested without a
            decoder. Returning exact-only results in that case would present "we could not look"
            as "we looked and found nothing".
    """
    dir_a, dir_b = Path(capsule_a), Path(capsule_b)
    for label, path in (("A", dir_a), ("B", dir_b)):
        if not path.is_dir():
            raise MediaDiffError(f"capsule {label} not found: {path}")
    if perceptual and decoder is None:
        raise MediaDiffError(
            "perceptual comparison was requested but no image decoder was supplied; refusing to "
            "return exact-only results under a perceptual flag, because that would report "
            "'no near-duplicates' about a check that never ran"
        )
    if threshold < 0:
        raise MediaDiffError("threshold must not be negative")

    parts_a, parts_b = _parts(dir_a), _parts(dir_b)
    pairs: list[MediaPairDiff] = []
    for index in range(max(len(parts_a), len(parts_b))):
        part_a = parts_a[index] if index < len(parts_a) else None
        part_b = parts_b[index] if index < len(parts_b) else None
        hash_a = part_a.get("content_hash") if part_a else None
        hash_b = part_b.get("content_hash") if part_b else None
        media_type = (part_b or part_a or {}).get("media_type")

        if part_a is None:
            pairs.append(
                MediaPairDiff(index=index, verdict="added", hash_b=hash_b, media_type=media_type)
            )
            continue
        if part_b is None:
            pairs.append(
                MediaPairDiff(index=index, verdict="removed", hash_a=hash_a, media_type=media_type)
            )
            continue
        if hash_a is not None and hash_a == hash_b:
            pairs.append(
                MediaPairDiff(
                    index=index, verdict="identical", hash_a=hash_a, hash_b=hash_b,
                    media_type=media_type,
                )
            )
            continue

        distance: int | None = None
        reason: str | None = None
        if perceptual and decoder is not None:
            distance, reason = _perceptual_pair(
                dir_a, dir_b, part_a, part_b, decoder, threshold
            )
        near = distance is not None and distance <= threshold
        pairs.append(
            MediaPairDiff(
                index=index,
                verdict="near-duplicate" if near else "changed",
                hash_a=hash_a,
                hash_b=hash_b,
                media_type=media_type,
                hamming=distance,
                threshold=threshold if perceptual else None,
                perceptual_unavailable=reason,
            )
        )

    return MediaDiff(
        pairing=PAIRING,
        perceptual=perceptual,
        parts_a=len(parts_a),
        parts_b=len(parts_b),
        pairs=pairs,
    )


__all__ = [
    "DEFAULT_HAMMING_THRESHOLD",
    "PAIRING",
    "SCHEMA_VERSION",
    "Decoder",
    "MediaDiff",
    "MediaDiffError",
    "MediaPairDiff",
    "MIN_LOW_FREQUENCY_COMPONENTS",
    "MediaVerdict",
    "diff_media",
    "low_frequency_components",
    "hamming",
    "phash",
    "pillow_decoder",
]
