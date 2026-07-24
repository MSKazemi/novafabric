# Copyright 2024 NovaFabric Contributors
# Apache-2.0 License
"""Tests for DocumentRenderer (renderer.py coverage targets)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.compliance.export.models import AnnexIVDocument, AnnexIVElement
from novafabric.compliance.export.renderer import DocumentRenderer, _render_html


def _minimal_document() -> AnnexIVDocument:
    elements = [
        AnnexIVElement(
            element_id=i,
            element_title=f"Element {i}",
            population_method="capsule_derived",
            content=f"Content for element {i}",
            completeness_flag="complete",
            evidence_refs=[f"ref-{i}"] if i % 2 == 0 else [],
        )
        for i in range(1, 16)
    ]
    return AnnexIVDocument(
        deployment_id="test-deploy-001",
        generated_at=datetime.now(tz=timezone.utc).isoformat(),
        elements=elements,
    )


class TestRenderHtml:
    def test_produces_html_string(self) -> None:
        doc = _minimal_document()
        html = _render_html(doc)
        assert "<!DOCTYPE html>" in html
        assert "test-deploy-001" in html
        assert "Element 1" in html

    def test_evidence_source_marker_rendered(self) -> None:
        # ADR-0197: the provenance marker must be visible in the rendered doc.
        from novafabric.compliance.export.provenance import EvidenceSource

        doc = _minimal_document()
        doc.elements[0].evidence_source = EvidenceSource.capsule_verified
        html = _render_html(doc)
        assert "capsule_verified" in html
        assert "Provenance" in html

    def test_evidence_refs_included(self) -> None:
        doc = _minimal_document()
        html = _render_html(doc)
        assert "ref-2" in html

    def test_no_evidence_refs_empty_block(self) -> None:
        doc = _minimal_document()
        html = _render_html(doc)
        assert "evidence-refs" in html or "Evidence refs" in html or "ref-2" in html


class TestDocumentRenderer:
    def test_render_json_ld_only(self, tmp_path: Path) -> None:
        doc = _minimal_document()
        renderer = DocumentRenderer()
        json_ld_path, pdf_path = renderer.render(doc, tmp_path, pdf=False)
        assert json_ld_path.exists()
        assert pdf_path is None
        content = json_ld_path.read_text()
        assert "test-deploy-001" in content

    def test_render_creates_output_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "output"
        doc = _minimal_document()
        renderer = DocumentRenderer()
        json_ld_path, _ = renderer.render(doc, target, pdf=False)
        assert json_ld_path.exists()

    def test_render_pdf_raises_without_weasyprint(self, tmp_path: Path) -> None:
        doc = _minimal_document()
        renderer = DocumentRenderer()
        with patch.dict(sys.modules, {"weasyprint": None}):
            with pytest.raises(ImportError, match="WeasyPrint is not installed"):
                renderer.render(doc, tmp_path, pdf=True)

    def test_render_pdf_calls_weasyprint(self, tmp_path: Path) -> None:
        doc = _minimal_document()
        renderer = DocumentRenderer()

        mock_html_instance = MagicMock()
        mock_html_cls = MagicMock(return_value=mock_html_instance)
        mock_weasyprint = MagicMock()
        mock_weasyprint.HTML = mock_html_cls

        with patch.dict(sys.modules, {"weasyprint": mock_weasyprint}):
            json_ld_path, pdf_path = renderer.render(doc, tmp_path, pdf=True)

        assert json_ld_path.exists()
        assert pdf_path is not None
        mock_html_cls.assert_called_once()
        mock_html_instance.write_pdf.assert_called_once()

    def test_render_safe_filename(self, tmp_path: Path) -> None:
        doc = _minimal_document()
        doc = doc.model_copy(update={"deployment_id": "eu/prod/v1"})
        renderer = DocumentRenderer()
        json_ld_path, _ = renderer.render(doc, tmp_path, pdf=False)
        assert "/" not in json_ld_path.name
        assert "eu_prod_v1" in json_ld_path.name
