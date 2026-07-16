"""Shareable capsule viewer (ADR-0140) — single-file offline HTML export.

Projects a Run Capsule into a bounded, redaction-preserving ``CapsuleView``
summary (spec: ``design/spec/capsule-viewer-v0.md``, schema:
``schemas/capsule-view.schema.json``) and renders it as **one** self-contained
HTML file: inline CSS, no JavaScript, zero external requests, opens from
``file://``. Experimental.
"""

from novafabric.viewer.html import export_capsule_html, render_capsule_view_html
from novafabric.viewer.view import (
    CAPSULE_VIEW_SCHEMA_VERSION,
    CapsuleViewResult,
    build_capsule_view,
)

__all__ = [
    "CAPSULE_VIEW_SCHEMA_VERSION",
    "CapsuleViewResult",
    "build_capsule_view",
    "export_capsule_html",
    "render_capsule_view_html",
]
