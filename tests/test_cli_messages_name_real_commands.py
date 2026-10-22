"""L5/L8: an error message that tells the user what to run must name a command that works.

L5: ``nova export-evidence`` without ``--key`` told users to run
``python -m novafabric.evidence.signing`` — a module with no ``__main__``: it exits 0
and writes nothing. The key the export wants is the one ``nova init`` writes.

L8: the promote refusal said "Run `novafabric eval` first"; there is no such command.
The suites a promotion checks are run by ``nova eval agent <name@version>``.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.registry.service import PromotionBlockedError, promote_asset

runner = CliRunner()
FIXTURES = Path(__file__).parent / "fixtures"


def test_export_evidence_without_key_points_at_nova_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(home))
    capsule = CaptureOrchestrator(base_dir=tmp_path / "runs").run(
        [sys.executable, "-c", "pass"]
    ).capsule_dir

    refused = runner.invoke(
        app, ["export-evidence", str(capsule), "--output", str(tmp_path / "b.zip")]
    )
    assert isinstance(refused.exception, SystemExit), refused.exception
    assert refused.exit_code == 1
    text = " ".join(refused.output.split())
    assert "python -m novafabric.evidence.signing" not in text
    assert "nova init" in text

    # Following the advice works: `nova init` writes exactly the key path named.
    init = runner.invoke(app, ["init"])
    assert init.exit_code == 0, init.output
    named = re.search(r"--key (\S+signing_key\.pem)", text)
    assert named, text
    key = Path(named.group(1))
    assert key.is_file()
    ok = runner.invoke(
        app, ["export-evidence", str(capsule), "--key", str(key), "--output", str(tmp_path / "b.zip")]
    )
    assert ok.exit_code == 0, ok.output


def test_promote_refusal_names_the_real_eval_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "reg.db"))
    assert runner.invoke(app, ["register", str(FIXTURES / "valid_agent.yaml")]).exit_code == 0
    with pytest.raises(PromotionBlockedError) as excinfo:
        promote_asset(
            "kube-rca-agent", "v1.0.0", "staging", actor="alice", db_path=tmp_path / "reg.db"
        )
    message = str(excinfo.value)
    assert "`novafabric eval`" not in message
    assert "nova eval agent kube-rca-agent@v1.0.0" in message
