from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.registry.service import register_asset
from novafabric.report.generator import generate_report
from novafabric.spec.validator import validate_spec

# ---------------------------------------------------------------------------
# Report generator — Markdown
# ---------------------------------------------------------------------------


def test_markdown_report_has_inventory_header(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)

    report = generate_report("markdown", db_path=tmp_db)
    assert "# NovaFabric Asset Inventory" in report


def test_markdown_report_groups_by_asset_type(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    for fname in ("valid_model.yaml", "valid_agent.yaml", "valid_prompt.yaml"):
        spec = validate_spec(fixtures_dir / fname)
        register_asset(spec, fixtures_dir / fname, db_path=tmp_db)

    report = generate_report("markdown", db_path=tmp_db)
    assert "## Agents" in report or "## Agent" in report.lower()
    assert "## Models" in report or "## Model" in report.lower()
    assert "## Prompts" in report or "## Prompt" in report.lower()
    assert "fraud-model" in report
    assert "kube-rca-agent" in report


def test_markdown_report_contains_asset_name_and_version(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)

    report = generate_report("markdown", db_path=tmp_db)
    assert "fraud-model" in report
    assert "1.0.0" in report


def test_markdown_report_empty_registry(tmp_db: Path) -> None:
    report = generate_report("markdown", db_path=tmp_db)
    assert "# NovaFabric Asset Inventory" in report
    # No asset rows — just the header
    assert "fraud-model" not in report


# ---------------------------------------------------------------------------
# Report generator — JSON
# ---------------------------------------------------------------------------


def test_json_report_is_valid_json(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)

    report = generate_report("json", db_path=tmp_db)
    data = json.loads(report)
    assert isinstance(data, list)
    assert len(data) == 1


def test_json_report_contains_asset_fields(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)

    report = generate_report("json", db_path=tmp_db)
    data = json.loads(report)
    asset = data[0]
    assert asset["name"] == "fraud-model"
    assert asset["version"] == "1.0.0"
    assert asset["asset_type"] == "model"
    assert asset["status"] == "development"


def test_json_report_excludes_spec_json_field(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)

    report = generate_report("json", db_path=tmp_db)
    data = json.loads(report)
    assert "spec_json" not in data[0]


def test_json_report_multiple_assets(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    for fname in ("valid_model.yaml", "valid_agent.yaml"):
        spec = validate_spec(fixtures_dir / fname)
        register_asset(spec, fixtures_dir / fname, db_path=tmp_db)

    report = generate_report("json", db_path=tmp_db)
    data = json.loads(report)
    assert len(data) == 2
    names = {a["name"] for a in data}
    assert "fraud-model" in names
    assert "kube-rca-agent" in names


def test_report_works_without_network(
    tmp_db: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Report must work offline — no HTTP calls, only SQLite."""
    import socket

    def no_network(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("network disabled")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)

    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)

    report = generate_report("markdown", db_path=tmp_db)
    assert "fraud-model" in report


# ---------------------------------------------------------------------------
# git adapter
# ---------------------------------------------------------------------------


def test_git_adapter_returns_sha_or_none() -> None:
    from novafabric.adapters.git import get_head_sha

    result = get_head_sha()
    assert result is None or (isinstance(result, str) and len(result) == 40)


def test_git_adapter_safe_fallback_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a non-git directory, must return None without raising."""
    import subprocess

    from novafabric.adapters.git import get_head_sha

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = get_head_sha()
    assert result is None


# ---------------------------------------------------------------------------
# mlflow adapter stub
# ---------------------------------------------------------------------------


def test_mlflow_adapter_returns_none_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    from novafabric.adapters.mlflow import validate_run_id

    result = validate_run_id("some-run-id")
    assert result is None


def test_mlflow_adapter_returns_truthy_with_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
    from novafabric.adapters.mlflow import validate_run_id

    result = validate_run_id("some-run-id")
    assert result is not None


# ---------------------------------------------------------------------------
# langfuse adapter stub
# ---------------------------------------------------------------------------


def test_langfuse_adapter_returns_none_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    from novafabric.adapters.langfuse import resolve_prompt_uri

    result = resolve_prompt_uri("langfuse://my-prompt")
    assert result is None


def test_langfuse_adapter_returns_uri_with_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_HOST", "https://cloud.langfuse.com")
    from novafabric.adapters.langfuse import resolve_prompt_uri

    uri = "langfuse://my-prompt"
    result = resolve_prompt_uri(uri)
    assert result == uri


# ---------------------------------------------------------------------------
# Report generator — HTML + PDF (ADR-0201)
# ---------------------------------------------------------------------------


def test_html_report_self_contained_with_chart(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    for fname in ("valid_model.yaml", "valid_agent.yaml"):
        spec = validate_spec(fixtures_dir / fname)
        register_asset(spec, fixtures_dir / fname, db_path=tmp_db)

    html = generate_report("html", db_path=tmp_db)
    assert html.startswith("<!DOCTYPE html>")
    assert "NovaFabric Asset Inventory" in html
    assert "<svg" in html  # assets-by-type bar chart
    assert "report-data" in html  # embedded canonical JSON
    for scheme in ("http://", "https://"):
        assert scheme not in html  # self-contained, no external requests
    assert "fraud-model" in html


def test_html_report_empty_registry_no_chart(tmp_db: Path) -> None:
    html = generate_report("html", db_path=tmp_db)
    assert "<svg" not in html
    assert "no rows matched" in html


def test_pdf_report_hint_without_weasyprint(
    tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    from novafabric.report.generator import generate_report_pdf

    real_import = builtins.__import__

    def _no_weasyprint(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "weasyprint":
            raise ImportError("absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_weasyprint)
    with pytest.raises(RuntimeError, match=r"novafabric\[compliance\]"):
        generate_report_pdf(db_path=tmp_db)


def test_pdf_report_bytes_when_weasyprint_present(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    pytest.importorskip("weasyprint")
    from novafabric.report.generator import generate_report_pdf

    spec = validate_spec(fixtures_dir / "valid_model.yaml")
    register_asset(spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)
    pdf = generate_report_pdf(db_path=tmp_db)
    assert pdf.startswith(b"%PDF")
