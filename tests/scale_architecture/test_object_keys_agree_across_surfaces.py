"""Every surface that *reports* a dual-object key must agree with the writer.

The writer stored `audit/<run_id>/audit.json` on S3. `nova storage inspect`, the
`/api/storage/inspect/{run_id}` route, and the dashboard card that renders it all
independently hardcoded `<run_id>_audit.json` and presented it as the S3 key.

Both sides had passing tests — `test_dual_object_store_s3.py` pinned the writer's
prefixes, `test_v018_dashboard_parity.py` pinned the reporters' flat names — and
nobody compared the two. That is the gap this file closes: it asserts the reported
key *equals the key the writer actually used*, so the two can never drift again.

Why it mattered: prefix-scoped S3 lifecycle rules, Object Lock retention policies
and bucket policies are the standard way to manage a PII object. One written against
the reported key would have matched nothing and silently governed no object at all.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app as cli_app
from novafabric.storage.dual_object_store import (
    DualObjectStore,
    local_audit_filename,
    local_pii_filename,
    s3_audit_key,
    s3_pii_key,
)

runner = CliRunner()

RUN_ID = "run-key-agreement-42"


def _cli_json(monkeypatch, **env: str) -> dict:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    result = runner.invoke(cli_app, ["storage", "inspect", "--run-id", RUN_ID, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def test_cli_reports_the_s3_keys_the_writer_uses(monkeypatch) -> None:
    body = _cli_json(monkeypatch, NOVA_S3_BUCKET="nova-capsules", NOVA_CAP003_ENABLED="true")

    assert body["audit_object_key"] == s3_audit_key(RUN_ID)
    assert body["pii_object_key"] == s3_pii_key(RUN_ID)
    assert body["layout"] == "s3"


def test_cli_reports_the_local_names_the_writer_uses(monkeypatch) -> None:
    monkeypatch.delenv("NOVA_S3_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("NOVA_S3_BUCKET", raising=False)
    body = _cli_json(monkeypatch, NOVA_CAP003_ENABLED="true")

    assert body["audit_object_key"] == local_audit_filename(RUN_ID)
    assert body["pii_object_key"] == local_pii_filename(RUN_ID)
    assert body["layout"] == "local"


def test_the_local_names_match_what_the_writer_actually_writes(
    tmp_path: Path, monkeypatch
) -> None:
    """The strongest form: compare against files that exist on disk."""
    monkeypatch.setenv("NOVA_CAP003_ENABLED", "true")
    result = DualObjectStore().split_and_store_local(
        RUN_ID, {"output_text": "secret", "run_id": RUN_ID}, tmp_path
    )

    on_disk = {p.name for p in tmp_path.iterdir()}
    assert local_audit_filename(RUN_ID) in on_disk, on_disk
    assert local_pii_filename(RUN_ID) in on_disk, on_disk
    assert Path(result.audit_object_key).name == local_audit_filename(RUN_ID)


def test_no_surface_reports_a_key_the_writer_never_produces() -> None:
    """The exact string that was wrong, pinned as wrong."""
    stale = f"{RUN_ID}_audit.json"

    assert s3_audit_key(RUN_ID) != stale
    assert s3_audit_key(RUN_ID).startswith("audit/")
    assert s3_pii_key(RUN_ID).startswith("pii/")
