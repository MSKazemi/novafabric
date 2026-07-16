"""ADR-0169 D1 / NF-375 — the ``nova export-whistleblower`` CLI.

Read-only. Renders a source-protecting whistleblower attestation over a sealed bundle. Exit 0 on
render; 2 on missing/malformed input **or a supplied source-identifying field** (the export refuses
to carry it — source protection is a hard error, not a warning).
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

_DOC = {
    "content_digest": "sha256:" + "a" * 64,
    "authenticity_attestation": "bundle://root/sig#ed25519",
    "anonymity_set_ref": "anonset://group/42",
}


def _write(tmp_path: Path, doc) -> Path:
    p = tmp_path / "wb.json"
    p.write_text(json.dumps(doc))
    return p


def test_renders_attestation(tmp_path):
    result = runner.invoke(app, ["export-whistleblower", str(_write(tmp_path, _DOC))])
    assert result.exit_code == 0, result.output
    assert "anonset://group/42" in result.output


def test_json_carries_digest_and_signature(tmp_path):
    result = runner.invoke(
        app, ["export-whistleblower", str(_write(tmp_path, _DOC)), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["content_digest"] == _DOC["content_digest"]
    assert payload["authenticity_attestation"] == "bundle://root/sig#ed25519"
    # No source-identifying key can appear in the output.
    assert not any(
        k in payload for k in ("source", "submitter", "email", "ip_address", "contact")
    )


def test_source_identifying_field_is_rejected(tmp_path):
    doc = dict(_DOC)
    doc["submitter_email"] = "leaker@example.com"
    result = runner.invoke(app, ["export-whistleblower", str(_write(tmp_path, doc))])
    assert result.exit_code == 2
    assert "submitter_email" in result.output


def test_missing_required_exits_two(tmp_path):
    result = runner.invoke(
        app, ["export-whistleblower", str(_write(tmp_path, {"content_digest": "d"}))]
    )
    assert result.exit_code == 2


def test_missing_file_exits_two(tmp_path):
    result = runner.invoke(app, ["export-whistleblower", str(tmp_path / "nope.json")])
    assert result.exit_code == 2


def test_malformed_exits_two(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope")
    result = runner.invoke(app, ["export-whistleblower", str(p)])
    assert result.exit_code == 2
