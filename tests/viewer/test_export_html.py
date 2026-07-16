"""Single-file offline HTML export tests (ADR-0140 P2) — renderer + CLI.

Guards the normative contract of design/spec/capsule-viewer-v0.md Part 2:
exactly one file, zero external requests, embedded inspectable JSON, no-JS
static rendering, redaction preserved.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.viewer.html import export_capsule_html, render_capsule_view_html
from novafabric.viewer.view import build_capsule_view
from viewer.conftest import REDACTION_MARKER, RUN_ID, SECRET_ARGUMENT_VALUE

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "capsule-view.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())

runner = CliRunner()


def _export(golden_capsule_dir: Path, tmp_path: Path) -> str:
    out = tmp_path / "out" / "capsule.html"
    written, warnings = export_capsule_html(golden_capsule_dir, out)
    assert written == out
    assert warnings == []
    return out.read_text(encoding="utf-8")


def _embedded_json(html: str) -> dict:
    match = re.search(
        r'<script type="application/json" id="capsule-view-data">\s*(.*?)\s*</script>',
        html,
        re.DOTALL,
    )
    assert match is not None, "embedded capsule-view-data block missing"
    return json.loads(match.group(1))


# ── single-file / offline invariant (the hard contract) ──────────────────────


def test_exactly_one_file_no_sidecars(golden_capsule_dir: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    export_capsule_html(golden_capsule_dir, out_dir / "capsule.html")
    assert [p.name for p in out_dir.iterdir()] == ["capsule.html"]


def test_no_external_references(golden_capsule_dir: Path, tmp_path: Path) -> None:
    """Zero external requests: no URLs, no linked assets, no network APIs."""
    html = _export(golden_capsule_dir, tmp_path)
    assert "http://" not in html
    assert "https://" not in html
    for forbidden in (
        "<link",  # no external stylesheets/fonts/icons
        "src=",  # no external (or any) script/img/iframe sources
        "href=",  # no anchors/links needed in v0 at all
        "@import",
        "url(",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
        "<iframe",
    ):
        assert forbidden not in html, f"external-capable construct found: {forbidden}"


def test_no_executable_javascript(golden_capsule_dir: Path, tmp_path: Path) -> None:
    """No-JS page: the only <script> is the inert application/json data block."""
    html = _export(golden_capsule_dir, tmp_path)
    scripts = re.findall(r"<script[^>]*>", html)
    assert scripts == ['<script type="application/json" id="capsule-view-data">']


def test_css_is_inline(golden_capsule_dir: Path, tmp_path: Path) -> None:
    html = _export(golden_capsule_dir, tmp_path)
    assert "<style>" in html


# ── embedded data ─────────────────────────────────────────────────────────────


def test_embedded_json_parses_and_validates(golden_capsule_dir: Path, tmp_path: Path) -> None:
    view = _embedded_json(_export(golden_capsule_dir, tmp_path))
    Draft202012Validator(_SCHEMA, format_checker=FormatChecker()).validate(view)
    assert view["capsule"]["run_id"] == RUN_ID


def test_embedded_json_cannot_break_out_of_script_block(tmp_path: Path, golden_capsule_dir: Path) -> None:
    """A '</script>' payload in capsule data must not terminate the data block."""
    view = build_capsule_view(golden_capsule_dir, title="</script><script>alert(1)").view
    html = render_capsule_view_html(view)
    assert "<script>alert(1)" not in html
    embedded = _embedded_json(html)
    assert embedded["title"] == "</script><script>alert(1)"


# ── rendered sections ─────────────────────────────────────────────────────────


def test_sections_rendered(golden_capsule_dir: Path, tmp_path: Path) -> None:
    html = _export(golden_capsule_dir, tmp_path)
    for expected in (
        RUN_ID,
        "claude-x-20260101",
        "web_search",
        "gaia",
        "dataset:sha256:1234",
        "Model calls (2)",
        "Tool calls (2)",
        "Eval scores (2)",
        "Lineage references",
        "not</strong> a cryptographic verifier",
    ):
        assert expected in html, f"missing from rendered page: {expected}"


def test_empty_sections_say_none_recorded(empty_capsule_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "empty.html"
    export_capsule_html(empty_capsule_dir, out)
    html = out.read_text(encoding="utf-8")
    assert html.count("none recorded") == 4  # model calls, tool calls, scores, lineage


def test_title_override(golden_capsule_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "titled.html"
    export_capsule_html(golden_capsule_dir, out, title="Nightly agent run")
    assert "<title>Nightly agent run</title>" in out.read_text(encoding="utf-8")


# ── redaction invariant in the rendered page ─────────────────────────────────


def test_secret_argument_never_in_html(golden_capsule_dir: Path, tmp_path: Path) -> None:
    html = _export(golden_capsule_dir, tmp_path)
    assert SECRET_ARGUMENT_VALUE not in html


def test_redaction_marker_verbatim_in_html(golden_capsule_dir: Path, tmp_path: Path) -> None:
    html = _export(golden_capsule_dir, tmp_path)
    assert REDACTION_MARKER in html  # shown exactly as the capsule stored it


# ── CLI (`nova export --html`) ────────────────────────────────────────────────


def test_cli_help_mentions_html(  # smoke: `nova export --help`
) -> None:
    result = runner.invoke(app, ["export", "--help"])
    assert result.exit_code == 0
    assert "--html" in result.output


def test_cli_export_html_smoke(golden_capsule_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "cli.html"
    result = runner.invoke(app, ["export", str(golden_capsule_dir), "--html", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "capsule viewer written" in result.output
    assert "nova verify" in result.output


def test_cli_default_output_next_to_capsule(golden_capsule_dir: Path) -> None:
    result = runner.invoke(app, ["export", str(golden_capsule_dir), "--html"])
    assert result.exit_code == 0, result.output
    assert (golden_capsule_dir.parent / f"{golden_capsule_dir.name}.html").exists()


def test_cli_without_html_flag_errors(golden_capsule_dir: Path) -> None:
    result = runner.invoke(app, ["export", str(golden_capsule_dir)])
    assert result.exit_code == 2
    assert "--html" in result.output


def test_cli_missing_capsule_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["export", str(tmp_path / "nope"), "--html"])
    assert result.exit_code == 1
    assert "not found" in result.output


def test_cli_warns_on_skipped_section(golden_capsule_dir: Path, tmp_path: Path) -> None:
    (golden_capsule_dir / "scores.jsonl").write_text("{bad\n")
    out = tmp_path / "warn.html"
    result = runner.invoke(app, ["export", str(golden_capsule_dir), "--html", "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "scores.jsonl" in result.output
    assert out.exists()
