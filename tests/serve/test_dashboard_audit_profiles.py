"""The dashboard's audit-profile list is the server's list (review frontend-dashboard-08).

Four panels each carried their own copy; ``eu-ai-act``, ``iso-42001``, ``nis2`` and the
Audit Map default ``eu_ai_act`` are not profile IDs and every one raised ``ValueError``.
"""

from __future__ import annotations

import re
from pathlib import Path

from novafabric.compliance.audit.loader import list_profiles

COMPLIANCE = (
    Path(__file__).resolve().parents[2]
    / "ui" / "dashboard" / "src" / "components" / "dashboard" / "tabs" / "compliance"
)
SHARED = COMPLIANCE / "auditProfiles.ts"
PANELS = ["AuditPanel", "AuditCoveragePanel", "AuditBundlePanel", "AuditMapPanel"]


def test_the_shared_list_is_exactly_the_servers_built_in_profiles() -> None:
    text = SHARED.read_text(encoding="utf-8")
    block = text[text.index("AUDIT_PROFILE_IDS = ["):].split("] as const", 1)[0]
    assert re.findall(r"'([^']+)'", block) == list_profiles()


def test_every_panel_uses_the_shared_list_and_keeps_no_copy() -> None:
    for name in PANELS:
        text = (COMPLIANCE / f"{name}.tsx").read_text(encoding="utf-8")
        assert "AUDIT_PROFILE_IDS" in text, name
        assert "'eu-ai-act'" not in text and "'iso-42001'" not in text, name
        assert "eu_ai_act" not in text, name
