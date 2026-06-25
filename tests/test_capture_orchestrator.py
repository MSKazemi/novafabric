import json
import sys
from pathlib import Path

import jsonschema
import yaml

from novafabric.capture.orchestrator import CaptureOrchestrator

SCHEMA_DIR = Path(__file__).parents[1] / "src/novafabric/schemas"


def _schema(name: str) -> dict:  # type: ignore[type-arg]
    return json.loads((SCHEMA_DIR / name).read_text())


def test_capture_simple_script_exits_0(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("print('hello')\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    assert result.exit_code == 0


def test_capture_creates_valid_capsule_yaml(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])

    manifest = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())
    jsonschema.validate(manifest, _schema("run-capsule.schema.json"),
                        format_checker=jsonschema.FormatChecker())


def test_capture_status_success(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    manifest = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())
    assert manifest["status"] == "success"


def test_capture_failing_script_status_failure(tmp_path: Path) -> None:
    script = tmp_path / "fail.py"
    script.write_text("import sys; sys.exit(1)\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    assert result.exit_code == 1
    manifest = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())
    assert manifest["status"] == "failure"
    assert manifest["exit_code"] == 1
    assert "error" in manifest


def test_capture_creates_required_files(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    for fname in [
        "capsule.yaml", "trace.jsonl", "env.lock",
        "model-calls.jsonl", "tool-calls.jsonl",
        "redaction-proof.json", "replay.yaml", "assets.jsonl",
    ]:
        assert (result.capsule_dir / fname).exists(), f"missing: {fname}"
    assert (result.capsule_dir / "inputs").is_dir()
    assert (result.capsule_dir / "outputs").is_dir()


def test_capture_env_lock_validates(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    env_lock = yaml.safe_load((result.capsule_dir / "env.lock").read_text())
    jsonschema.validate(env_lock, _schema("environment.schema.json"),
                        format_checker=jsonschema.FormatChecker())


def test_capture_redaction_proof_validates(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    proof = json.loads((result.capsule_dir / "redaction-proof.json").read_text())
    jsonschema.validate(proof, _schema("secret-redaction.schema.json"),
                        format_checker=jsonschema.FormatChecker())


def test_capture_run_id_is_ulid(tmp_path: Path) -> None:
    import re
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    assert re.match(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$", result.run_id)


def test_capture_capsule_dir_property(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)])
    assert result.capsule_dir.name == result.run_id
