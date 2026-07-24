"""Report catalog + HTML/PDF export routes (ADR-0200/0201, ADR-0183 pattern).

- ``GET /api/reports/catalog`` — the report registry serialized for the web
  client (identity, filters, chart specs), the single source of truth the
  ReportsTab reads for preview charts.
- ``GET /api/reports/{report_id}/export?format=html|pdf`` — a self-contained
  report artifact with an inline-SVG chart where the registry declares one.
  HTML needs nothing; PDF lazily imports WeasyPrint (optional extra) and
  degrades to ``501`` with the install hint when absent, mirroring the
  compliance renderer's pattern.
- ``GET /api/reports/{alert-digest,api-key-inventory,dashboard-audit,
  compliance-posture}?format=json|csv`` — plain data routes for the R4
  registry-only reports, mirroring the legacy ``app.py`` report routes'
  ``{columns, rows, count}`` JSON shape and CSV Content-Disposition. They are
  literal paths registered before the parameterized ``/{report_id}/export``
  route (FastAPI matches in registration order).

Built by a factory so the caller injects its auth dependency (ADR-0183 §3).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from novafabric.serve.report_registry import REPORTS, ReportSpec
from novafabric.serve.reports import rows_to_csv
from novafabric.viz.report_html import render_report_html

_PDF_HINT = (
    "PDF export requires WeasyPrint — install the optional extra: "
    "pip install 'novafabric[compliance]'"
)

#: Registry-only reports (R4) that get plain JSON/CSV data routes here; the
#: pre-registry reports keep their legacy routes in ``app.py``.
_DATA_ROUTE_IDS = (
    "alert-digest",
    "api-key-inventory",
    "dashboard-audit",
    "compliance-posture",
)


def _whitelisted_filters(spec: ReportSpec, request: Request) -> dict[str, str]:
    """Filters declared by the spec, validated; 422 on missing required ones.

    Everything else (including the auth token) is ignored — filters are
    echoed into artifacts and must stay a closed set.
    """
    filters = {
        k: request.query_params[k]
        for k in spec.filter_keys
        if request.query_params.get(k)
    }
    missing = [k for k in spec.required_filters if not filters.get(k)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"missing required filter(s): {', '.join(missing)}",
        )
    return filters


def build_report_export_router(
    verify_token: Callable[..., Any],
    *,
    capsule_dir: Path,
    db_path: Path | None,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(verify_token)], tags=["reports"])

    @router.get("/api/reports/catalog")
    async def report_catalog() -> dict[str, Any]:
        return {"reports": [spec.catalog_entry() for spec in REPORTS.values()]}

    def _register_data_route(rid: str) -> None:
        spec = REPORTS[rid]

        async def report_data(request: Request, format: str = "json") -> Response:
            if format not in ("json", "csv"):
                raise HTTPException(
                    status_code=422, detail="format must be 'json' or 'csv'"
                )
            filters = _whitelisted_filters(spec, request)
            columns, rows = spec.run(capsule_dir, db_path, filters)
            if format == "csv":
                return Response(
                    content=rows_to_csv(columns, rows),
                    media_type="text/csv",
                    headers={
                        "Content-Disposition": f'attachment; filename="{rid}.csv"'
                    },
                )
            return JSONResponse(
                {"columns": columns, "rows": rows, "count": len(rows)}
            )

        # Literal paths, registered before /{report_id}/export below.
        router.get(f"/api/reports/{rid}", name=f"report_{rid.replace('-', '_')}")(
            report_data
        )

    for _rid in _DATA_ROUTE_IDS:
        _register_data_route(_rid)

    @router.get("/api/reports/{report_id}/export")
    async def report_export(
        report_id: str,
        request: Request,
        format: str = "html",
    ) -> Response:
        spec = REPORTS.get(report_id)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"unknown report: {report_id}")
        if format not in ("html", "pdf"):
            raise HTTPException(
                status_code=422, detail="format must be 'html' or 'pdf'"
            )
        filters = _whitelisted_filters(spec, request)
        columns, rows = spec.run(capsule_dir, db_path, filters)
        chart_svg = spec.chart.render(rows, filters) if spec.chart else None
        html = render_report_html(
            title=spec.title,
            columns=columns,
            rows=rows,
            chart_svg=chart_svg,
            chart_title=spec.chart.title if (spec.chart and chart_svg) else None,
            filters=filters,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        if format == "html":
            return HTMLResponse(
                html,
                headers={
                    "Content-Disposition": (
                        f'attachment; filename="{report_id}.html"'
                    )
                },
            )
        try:
            from weasyprint import HTML  # noqa: PLC0415
        except ImportError as exc:
            raise HTTPException(status_code=501, detail=_PDF_HINT) from exc
        pdf_bytes = HTML(string=html).write_pdf()
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{report_id}.pdf"'
            },
        )

    return router
