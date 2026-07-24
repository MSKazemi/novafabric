"""R3 (ADR-0200/0201) — report catalog + HTML/PDF export router.

Covers: token guard, catalog shape, self-contained HTML artifacts (chart
present where the registry declares one, absent where it does not), the
row-cap truncation line, required-filter validation, unknown report /
format 422s, and the WeasyPrint-absent 501 degradation path.
"""
from __future__ import annotations

import builtins
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from novafabric.registry.runs_cache import ensure_runs_cache, upsert_run
from novafabric.serve.report_registry import REPORTS
from novafabric.viz.report_html import render_report_html

TOKEN = "testtoken"
H = {"host": "127.0.0.1:4321"}


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    from novafabric.serve.app import create_app

    db = tmp_path / "registry.db"
    con = sqlite3.connect(str(db))
    ensure_runs_cache(con)
    for i, (status, exit_code) in enumerate(
        [("success", 0), ("failure", 1), ("success", 0)]
    ):
        upsert_run(
            con,
            {
                "run_id": f"run-{i}",
                "status": status,
                "created_at": f"2026-07-0{i + 1}T08:00:00Z",
                "duration_ms": 100.0 * (i + 1),
                "exit_code": exit_code,
                "model_call_count": i,
                "tool_call_count": 1,
                "command": ["agent-x"],
            },
        )
    con.commit()
    con.close()

    base = tmp_path / "capsules"
    base.mkdir()
    app = create_app(token=TOKEN, capsule_dir=base, db_path=db, static_dir=None)
    return TestClient(app)


def test_export_requires_token(client: TestClient) -> None:
    c = client
    assert c.get("/api/reports/catalog", headers=H).status_code == 401
    assert c.get("/api/reports/throughput/export", headers=H).status_code == 401


def test_catalog_lists_registry(client: TestClient) -> None:
    c = client
    r = c.get(f"/api/reports/catalog?token={TOKEN}", headers=H)
    assert r.status_code == 200
    entries = {e["report_id"]: e for e in r.json()["reports"]}
    assert set(entries) == set(REPORTS)
    assert entries["throughput"]["chart"]["kind"] == "stacked-bar"
    assert entries["executive-summary"]["chart"] is None
    assert entries["capsule-compare"]["required_filters"] == ["run_a", "run_b"]


def test_html_export_is_self_contained_with_chart(
    client: TestClient,
) -> None:
    c = client
    r = c.get(f"/api/reports/throughput/export?token={TOKEN}&format=html", headers=H)
    assert r.status_code == 200
    assert r.headers["content-disposition"].endswith('filename="throughput.html"')
    html = r.text
    assert "<svg" in html  # chart declared for throughput
    assert "report-data" in html  # embedded canonical JSON
    for scheme in ("http://", "https://"):
        assert scheme not in html  # no external requests
    assert "<script src" not in html  # no JS


def test_html_export_without_chart(client: TestClient) -> None:
    c = client
    r = c.get(f"/api/reports/executive-summary/export?token={TOKEN}&format=html", headers=H)
    assert r.status_code == 200
    assert "<svg" not in r.text


def test_eval_regression_chart_needs_suite_filter(
    client: TestClient,
) -> None:
    c = client
    r = c.get(f"/api/reports/eval-regression/export?token={TOKEN}&format=html", headers=H)
    assert r.status_code == 200
    assert "<svg" not in r.text  # no single-suite filter → no line chart


def test_required_filters_enforced(client: TestClient) -> None:
    c = client
    r = c.get(f"/api/reports/capsule-compare/export?token={TOKEN}", headers=H)
    assert r.status_code == 422
    assert "run_a" in r.json()["detail"]


def test_unknown_report_and_format(client: TestClient) -> None:
    c = client
    assert c.get(f"/api/reports/nope/export?token={TOKEN}", headers=H).status_code == 404
    assert (
        c.get(f"/api/reports/throughput/export?token={TOKEN}&format=docx", headers=H).status_code
        == 422
    )


def test_pdf_degrades_501_without_weasyprint(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = client
    real_import = builtins.__import__

    def _no_weasyprint(name: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if name == "weasyprint":
            raise ImportError("weasyprint absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_weasyprint)
    r = c.get(f"/api/reports/throughput/export?token={TOKEN}&format=pdf", headers=H)
    assert r.status_code == 501
    assert "novafabric[compliance]" in r.json()["detail"]


def test_pdf_happy_path_when_weasyprint_present(
    client: TestClient,
) -> None:
    pytest.importorskip("weasyprint")
    c = client
    r = c.get(f"/api/reports/throughput/export?token={TOKEN}&format=pdf", headers=H)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")


def test_row_cap_truncation_line() -> None:
    rows = [{"a": i} for i in range(20)]
    html = render_report_html(
        title="T", columns=["a"], rows=rows, row_cap=5, generated_at="now"
    )
    assert "showing first 5 of 20" in html
    # untruncated render has no truncation line
    html2 = render_report_html(
        title="T", columns=["a"], rows=rows[:3], row_cap=5, generated_at="now"
    )
    assert "showing first" not in html2
