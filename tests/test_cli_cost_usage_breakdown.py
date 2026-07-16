"""ADR-0132 D3/D4 — the ``nova cost usage-breakdown`` CLI."""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()


def _write(tmp_path: Path, name: str, doc) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(doc))
    return p


def test_reads_bare_usage_totals(tmp_path):
    doc = {"input_tokens": 60, "output_tokens": 40, "reasoning_tokens": 10}
    result = runner.invoke(
        app, ["cost", "usage-breakdown", str(_write(tmp_path, "u.json", doc)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["counted_tokens"] == 110
    assert payload["has_reasoning_tokens"] is True


def test_reads_from_manifest_usage_totals_key(tmp_path):
    manifest = {"schema_version": "1", "usage_totals": {"input_tokens": 100, "cached_tokens": 25}}
    result = runner.invoke(
        app, ["cost", "usage-breakdown", str(_write(tmp_path, "manifest.json", manifest)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["cached_read_ratio"] == 0.25


def test_render_exit_zero(tmp_path):
    doc = {"input_tokens": 50, "audio_input_tokens": 50}
    result = runner.invoke(app, ["cost", "usage-breakdown", str(_write(tmp_path, "u.json", doc))])
    assert result.exit_code == 0, result.output
    assert "composition" in result.output.lower()


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["cost", "usage-breakdown", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_non_object_usage_totals_exits_two(tmp_path):
    doc = {"usage_totals": "nope"}
    result = runner.invoke(
        app, ["cost", "usage-breakdown", str(_write(tmp_path, "bad.json", doc))]
    )
    assert result.exit_code == 2
