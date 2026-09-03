"""NF-170 — ``nova diff --media`` (ADR-0148 D3).

Reports; does not gate: exit ``0`` whatever the classifications. ``--perceptual`` exits ``2``
when no decoder is available rather than silently returning exact-only results.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _capsule(tmp_path: Path, name: str, blobs: dict[str, bytes]) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    d.joinpath("capsule.json").write_text(json.dumps({"run_id": name}))
    content = [
        {
            "type": "image",
            "media": {
                "media_type": "image/png",
                "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "byte_size": len(raw),
                "redacted": False,
                "blob_ref": blob,
            },
        }
        for blob, raw in blobs.items()
    ]
    d.joinpath("model-calls.jsonl").write_text(
        json.dumps({"gen_ai.request.messages": [{"role": "user", "content": content}]}) + "\n"
    )
    for blob, raw in blobs.items():
        d.joinpath(blob).write_bytes(raw)
    return d


def test_identical_media_reports_identical(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", {"m.png": b"same"})
    b = _capsule(tmp_path, "b", {"m.png": b"same"})
    result = runner.invoke(app, ["diff", "--media", str(a), str(b), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [p["verdict"] for p in payload["pairs"]] == ["identical"]
    assert payload["pairing"] == "positional"


def test_changed_media_still_exits_zero(tmp_path: Path) -> None:
    """It reports; it does not gate."""
    a = _capsule(tmp_path, "a", {"m.png": b"one"})
    b = _capsule(tmp_path, "b", {"m.png": b"two"})
    result = runner.invoke(app, ["diff", "--media", str(a), str(b)])
    assert result.exit_code == 0, result.output
    assert "changed" in result.output


def test_the_text_output_names_the_pairing(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", {"m.png": b"one"})
    b = _capsule(tmp_path, "b", {"m.png": b"two"})
    flat = " ".join(runner.invoke(app, ["diff", "--media", str(a), str(b)]).output.split())
    assert "pairing positional" in flat


def test_added_and_removed_are_reported(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", {"m.png": b"one", "n.png": b"two"})
    b = _capsule(tmp_path, "b", {"m.png": b"one"})
    payload = json.loads(
        runner.invoke(app, ["diff", "--media", str(a), str(b), "--json"]).stdout
    )
    assert [p["verdict"] for p in payload["pairs"]] == ["identical", "removed"]


def test_a_missing_capsule_exits_two(tmp_path: Path) -> None:
    a = _capsule(tmp_path, "a", {"m.png": b"one"})
    result = runner.invoke(app, ["diff", "--media", str(a), str(tmp_path / "nope"), "--json"])
    assert result.exit_code == 2


def test_media_needs_two_paths(tmp_path: Path) -> None:
    result = runner.invoke(app, ["diff", "--media"])
    assert result.exit_code != 0


def test_perceptual_reports_a_distance_when_a_decoder_exists(tmp_path: Path) -> None:
    """Skipped where Pillow is absent — it is transitive here, not declared."""
    import pytest

    Image = pytest.importorskip("PIL.Image", reason="Pillow is not installed")
    import io
    import math

    def png(fx: int, fy: int) -> bytes:
        img = Image.new("L", (64, 64))
        img.putdata([
            int(128 + 100 * math.sin(2 * math.pi * (fx * x / 64 + fy * y / 64)))
            for y in range(64)
            for x in range(64)
        ])
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    a = _capsule(tmp_path, "a", {"m.png": png(2, 1)})
    b = _capsule(tmp_path, "b", {"m.png": png(6, 5)})
    result = runner.invoke(
        app, ["diff", "--media", "--perceptual", str(a), str(b), "--json"]
    )
    assert result.exit_code == 0, result.output
    pair = json.loads(result.stdout)["pairs"][0]
    assert pair["hamming"] is not None
    assert pair["verdict"] in {"changed", "near-duplicate"}
