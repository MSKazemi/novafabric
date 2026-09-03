"""NF-170 multi-modal media diff (ADR-0148 D3).

The pHash is tested on **synthetic pixel data**, so the algorithm is proven without needing an
image fixture or a decoder. The one test that does decode real bytes is skipped when Pillow is
absent — it is a transitive dependency here, not a declared one, so a test that assumed it would
be a test that breaks for a reason unrelated to this code.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import pytest

from novafabric.diff.media import (
    DEFAULT_HAMMING_THRESHOLD,
    MIN_LOW_FREQUENCY_COMPONENTS,
    PAIRING,
    MediaDiffError,
    diff_media,
    hamming,
    low_frequency_components,
    phash,
)

SIZE = 32


def _capsule(tmp_path: Path, name: str, parts: list[tuple[str, str]]) -> Path:
    """A capsule whose model call carries media parts as ``(content_hash, blob_name)``."""
    d = tmp_path / name
    d.mkdir(parents=True)
    d.joinpath("capsule.json").write_text(json.dumps({"run_id": name}))
    content = [
        {
            "type": "image",
            "media": {
                "media_type": "image/png",
                "content_hash": content_hash,
                "byte_size": 4,
                "redacted": False,
                "blob_ref": blob,
            },
        }
        for content_hash, blob in parts
    ]
    d.joinpath("model-calls.jsonl").write_text(
        json.dumps(
            {
                "model_call_id": "c1",
                "gen_ai.request.messages": [{"role": "user", "content": content}],
            }
        )
        + "\n"
    )
    return d


def _hash(n: int) -> str:
    return "sha256:" + f"{n:064x}"


# ── The pHash algorithm, on synthetic pixels ─────────────────────────────


def _wave(fx: float, fy: float, *, amp: float = 100.0, brightness: float = 0.0) -> list[float]:
    """A low-frequency 2-D sinusoid — the kind of structure a pHash is built to see."""
    return [
        128.0 + brightness + amp * math.sin(2 * math.pi * (fx * c / SIZE + fy * r / SIZE))
        for r in range(SIZE)
        for c in range(SIZE)
    ]


def _flat() -> list[float]:
    return [128.0] * (SIZE * SIZE)


def _checkerboard() -> list[float]:
    """All energy at the highest frequency — a pHash structurally cannot see it."""
    return [255.0 if (r + c) % 2 else 0.0 for r in range(SIZE) for c in range(SIZE)]


def test_the_phash_is_deterministic() -> None:
    assert phash(_wave(2, 1)) == phash(_wave(2, 1))


def test_a_uniformly_brighter_copy_hashes_identically() -> None:
    """Excluding the DC term from the median is what makes this exact rather than merely close."""
    assert hamming(phash(_wave(2, 1)), phash(_wave(2, 1, brightness=40.0))) == 0


def test_a_contrast_change_stays_within_the_threshold() -> None:
    """The stand-in for re-encoding: structure preserved, exact bytes not."""
    distance = hamming(phash(_wave(2, 1)), phash(_wave(2, 1, amp=95.0)))
    assert distance <= DEFAULT_HAMMING_THRESHOLD


@pytest.mark.parametrize("fx,fy", [(3, 2), (6, 5), (7, 3)])
def test_a_structurally_different_image_is_far(fx: int, fy: int) -> None:
    """The near-duplicate tests mean nothing unless the hash can also be far apart."""
    distance = hamming(phash(_wave(2, 1)), phash(_wave(fx, fy)))
    assert distance > DEFAULT_HAMMING_THRESHOLD


def test_numerical_dust_does_not_decide_the_bits() -> None:
    """Regression: without snapping near-zero coefficients, a brightness-only change — which
    perturbs nothing but the DC term — flipped 12 of 64 bits, because the median landed inside
    ±1e-14 of rounding error and half the hash was decided by noise."""
    for brightness in (1.0, 40.0, 500.0):
        assert hamming(phash(_wave(2, 1)), phash(_wave(2, 1, brightness=brightness))) == 0


def test_a_featureless_image_has_no_comparable_structure() -> None:
    assert low_frequency_components(_flat()) < MIN_LOW_FREQUENCY_COMPONENTS


def test_a_low_frequency_image_is_comparable() -> None:
    assert low_frequency_components(_wave(2, 1)) >= MIN_LOW_FREQUENCY_COMPONENTS


def test_the_wrong_pixel_count_is_refused() -> None:
    with pytest.raises(MediaDiffError, match="expected 1024 greyscale pixels"):
        phash([0.0, 1.0])


def test_hamming_counts_differing_bits() -> None:
    assert hamming(0b1011, 0b1001) == 1
    assert hamming(0, 0) == 0


# ── Exact diff — the default, no decoder involved ────────────────────────


def test_identical_parts(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [(_hash(1), "blobs/x.png")])
    b = _capsule(tmp_path, "b", [(_hash(1), "blobs/x.png")])
    result = diff_media(a, b)
    assert [p.verdict for p in result.pairs] == ["identical"]
    assert result.counts == {"identical": 1}


def test_changed_added_and_removed(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [(_hash(1), "x.png"), (_hash(2), "y.png")])
    b = _capsule(tmp_path, "b", [(_hash(9), "x.png")])
    result = diff_media(a, b)
    assert [p.verdict for p in result.pairs] == ["changed", "removed"]
    assert (result.parts_a, result.parts_b) == (2, 1)


def test_an_extra_part_on_the_right_is_added(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [(_hash(1), "x.png")])
    b = _capsule(tmp_path, "b", [(_hash(1), "x.png"), (_hash(3), "z.png")])
    assert [p.verdict for p in diff_media(a, b).pairs] == ["identical", "added"]


def test_the_pairing_basis_is_reported_not_implied(tmp_path: Path) -> None:
    """A wrong pairing produces confident nonsense, so it must not be invisible."""
    a = _capsule(tmp_path, "a", [(_hash(1), "x.png")])
    result = diff_media(a, _capsule(tmp_path, "b", [(_hash(1), "x.png")]))
    assert result.pairing == PAIRING == "positional"


def test_capsules_with_no_media_diff_to_nothing(tmp_path: Path) -> None:
    a, b = _capsule(tmp_path, "a", []), _capsule(tmp_path, "b", [])
    result = diff_media(a, b)
    assert result.pairs == []
    assert result.counts == {}


def test_a_missing_capsule_is_refused(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [])
    with pytest.raises(MediaDiffError, match="capsule B not found"):
        diff_media(a, tmp_path / "nope")


def test_the_result_carries_no_verdict_field(tmp_path: Path) -> None:
    a, b = _capsule(tmp_path, "a", []), _capsule(tmp_path, "b", [])
    fields = set(diff_media(a, b).model_dump().keys())
    assert not fields & {"regressed", "passed", "verdict", "ok"}


# ── Perceptual: opt-in, and honest when it cannot run ────────────────────


def _decoder_for(mapping: dict[bytes, list[float]]):
    def decode(raw: bytes) -> tuple[int, int, list[float]]:
        if raw not in mapping:
            raise MediaDiffError("could not decode media bytes: unknown fixture")
        return SIZE, SIZE, mapping[raw]

    return decode


def _with_blobs(tmp_path: Path, name: str, blobs: dict[str, bytes]) -> Path:
    # The content hash is over the bytes, as a real capsule records it — deriving it from the
    # file name instead made both sides match and the pair classified `identical`, so the
    # perceptual path under test never ran.
    d = _capsule(
        tmp_path,
        name,
        [("sha256:" + hashlib.sha256(raw).hexdigest(), blob) for blob, raw in blobs.items()],
    )
    for blob, raw in blobs.items():
        target = d / blob
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    return d


def test_perceptual_without_a_decoder_is_refused(tmp_path: Path) -> None:
    """Returning exact-only under a perceptual flag would report a check that never ran."""
    a, b = _capsule(tmp_path, "a", []), _capsule(tmp_path, "b", [])
    with pytest.raises(MediaDiffError, match="no image decoder was supplied"):
        diff_media(a, b, perceptual=True)


def test_a_near_duplicate_is_promoted_and_reports_its_distance(tmp_path: Path) -> None:
    a = _with_blobs(tmp_path, "a", {"m.png": b"AAA"})
    b = _with_blobs(tmp_path, "b", {"m.png": b"BBB"})
    decoder = _decoder_for({b"AAA": _wave(2, 1), b"BBB": _wave(2, 1, amp=95.0)})

    pair = diff_media(a, b, perceptual=True, decoder=decoder).pairs[0]
    assert pair.verdict == "near-duplicate"
    assert pair.hamming is not None and pair.hamming <= DEFAULT_HAMMING_THRESHOLD
    assert pair.threshold == DEFAULT_HAMMING_THRESHOLD


def test_a_genuinely_different_image_stays_changed(tmp_path: Path) -> None:
    a = _with_blobs(tmp_path, "a", {"m.png": b"AAA"})
    b = _with_blobs(tmp_path, "b", {"m.png": b"BBB"})
    decoder = _decoder_for({b"AAA": _wave(2, 1), b"BBB": _wave(6, 5)})

    pair = diff_media(a, b, perceptual=True, decoder=decoder).pairs[0]
    assert pair.verdict == "changed"
    assert pair.hamming is not None and pair.hamming > DEFAULT_HAMMING_THRESHOLD


def test_an_undecodable_pair_stays_changed_and_says_why(tmp_path: Path) -> None:
    """Never promoted on missing evidence, and never presented as though it had been checked."""
    a = _with_blobs(tmp_path, "a", {"m.png": b"AAA"})
    b = _with_blobs(tmp_path, "b", {"m.png": b"???"})
    decoder = _decoder_for({b"AAA": _wave(2, 1)})

    pair = diff_media(a, b, perceptual=True, decoder=decoder).pairs[0]
    assert pair.verdict == "changed"
    assert pair.hamming is None
    assert pair.perceptual_unavailable is not None
    assert "could not decode" in pair.perceptual_unavailable


def test_a_missing_blob_stays_changed_and_says_which_side(tmp_path: Path) -> None:
    a = _with_blobs(tmp_path, "a", {"m.png": b"AAA"})
    b = _capsule(tmp_path, "b", [(_hash(77), "m.png")])  # declared, never written
    decoder = _decoder_for({b"AAA": _wave(2, 1)})

    pair = diff_media(a, b, perceptual=True, decoder=decoder).pairs[0]
    assert pair.verdict == "changed"
    assert pair.perceptual_unavailable is not None
    assert "side B" in pair.perceptual_unavailable


def test_a_featureless_image_is_not_called_a_near_duplicate(tmp_path: Path) -> None:
    """The guard, exercised through the public path — not just its helper.

    A flat fill hashes like every other flat fill, so without this the two would be reported
    `near-duplicate` at distance 0: a confident answer produced by a coincidence.
    """
    a = _with_blobs(tmp_path, "a", {"m.png": b"AAA"})
    b = _with_blobs(tmp_path, "b", {"m.png": b"BBB"})
    decoder = _decoder_for({b"AAA": _flat(), b"BBB": [200.0] * (SIZE * SIZE)})

    pair = diff_media(a, b, perceptual=True, decoder=decoder).pairs[0]
    assert pair.verdict == "changed"
    assert pair.hamming is None
    assert pair.perceptual_unavailable is not None
    assert "low-frequency structure" in pair.perceptual_unavailable


def test_a_high_frequency_only_image_is_not_compared_either(tmp_path: Path) -> None:
    """A pHash structurally cannot see a fine checkerboard; any verdict would be a coincidence."""
    a = _with_blobs(tmp_path, "a", {"m.png": b"AAA"})
    b = _with_blobs(tmp_path, "b", {"m.png": b"BBB"})
    decoder = _decoder_for({b"AAA": _flat(), b"BBB": _checkerboard()})

    pair = diff_media(a, b, perceptual=True, decoder=decoder).pairs[0]
    assert pair.verdict == "changed"
    assert pair.perceptual_unavailable is not None


def test_a_part_with_no_blob_ref_cannot_be_compared(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", [(_hash(1), "")])
    b = _capsule(tmp_path, "b", [(_hash(2), "")])
    decoder = _decoder_for({})

    pair = diff_media(a, b, perceptual=True, decoder=decoder).pairs[0]
    assert pair.verdict == "changed"
    assert pair.perceptual_unavailable is not None
    assert "blob_ref absent" in pair.perceptual_unavailable


def test_identical_parts_are_not_perceptually_compared(tmp_path: Path) -> None:
    """Exact identity already settles it; decoding again would be work for no answer."""
    a = _with_blobs(tmp_path, "a", {"m.png": b"AAA"})
    b = _capsule(tmp_path, "b", [(a_hash := diff_media(a, a).pairs[0].hash_a, "m.png")])
    assert a_hash is not None

    def explode(raw: bytes) -> tuple[int, int, list[float]]:
        raise AssertionError("decoder must not be called for an identical pair")

    assert diff_media(a, b, perceptual=True, decoder=explode).pairs[0].verdict == "identical"


def test_a_negative_threshold_is_refused(tmp_path: Path) -> None:
    a, b = _capsule(tmp_path, "a", []), _capsule(tmp_path, "b", [])
    with pytest.raises(MediaDiffError, match="must not be negative"):
        diff_media(a, b, perceptual=True, decoder=_decoder_for({}), threshold=-1)


# ── One test against a real decoder, skipped when it is absent ───────────


def test_a_real_re_encode_is_a_near_duplicate(tmp_path: Path) -> None:
    """Pillow is transitive here, not declared — so this skips rather than fails without it."""
    Image = pytest.importorskip("PIL.Image", reason="Pillow is not installed")
    import io

    from novafabric.diff.media import pillow_decoder

    original = Image.new("L", (128, 128))
    original.putdata(
        [int(127 + 120 * math.sin((x + y) / 9.0)) for y in range(128) for x in range(128)]
    )
    lossless, lossy = io.BytesIO(), io.BytesIO()
    original.save(lossless, format="PNG")
    original.save(lossy, format="JPEG", quality=25)

    a = _with_blobs(tmp_path, "a", {"m.png": lossless.getvalue()})
    b = _with_blobs(tmp_path, "b", {"m.png": lossy.getvalue()})

    pair = diff_media(a, b, perceptual=True, decoder=pillow_decoder).pairs[0]
    assert pair.hash_a != pair.hash_b, "the bytes really do differ"
    assert pair.verdict == "near-duplicate", f"hamming was {pair.hamming}"
