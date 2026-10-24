"""A non-object JSON line is a reported error, never a traceback (review compliance-privacy-21).

``[1, 2]`` in the audit log (corruption, a manual edit) made ``AuditLog.verify()`` and
the SIEM export die with ``AttributeError: 'list' object has no attribute 'get'``
instead of reporting a chain error; the PROV-JSON exporter did the same on lineage.
"""

from __future__ import annotations

import json
from pathlib import Path

from novafabric.audit._log import AuditLog
from novafabric.audit._models import AuditEventType


def test_verify_reports_a_non_object_line(tmp_path: Path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    log.append(next(iter(AuditEventType)), "tester", "r1")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("[1, 2]\n")
    errors = log.verify()
    assert any("not a JSON object" in e for e in errors), errors


def test_prov_json_skips_a_non_object_lineage_line(tmp_path: Path) -> None:
    from novafabric.compliance.export import prov_json

    cap = tmp_path / "cap"
    cap.mkdir()
    edge = {"edge_type": "derived_from", "source_id": "a", "target_id": "b", "run_id": "r"}
    (cap / "lineage.jsonl").write_text("[1]\n" + json.dumps(edge) + "\n")
    doc = prov_json.export_prov_json(cap)  # must not raise
    assert "wasDerivedFrom" in json.dumps(doc)
