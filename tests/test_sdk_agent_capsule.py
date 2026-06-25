from pathlib import Path

import pytest
import yaml

from novafabric.sdk.agent import agent


def test_agent_without_capsule_dir_runs_normally(tmp_path: Path) -> None:
    @agent(name="my-agent", version="1.0")
    def greet() -> str:
        return "hello"

    assert greet() == "hello"


def test_agent_with_capsule_dir_creates_artifacts(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capsule"

    @agent(name="my-agent", version="1.0", capsule_dir=cap_dir)
    def noop() -> None:
        pass

    noop()

    for fname in [
        "capsule.yaml", "trace.jsonl", "env.lock",
        "model-calls.jsonl", "tool-calls.jsonl",
        "redaction-proof.json", "replay.yaml", "assets.jsonl",
    ]:
        assert (cap_dir / fname).exists(), f"missing: {fname}"
    assert (cap_dir / "inputs").is_dir()
    assert (cap_dir / "outputs").is_dir()


def test_agent_capsule_manifest_capture_mode(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capsule"

    @agent(name="my-agent", version="1.0", capsule_dir=cap_dir)
    def noop() -> None:
        pass

    noop()
    manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
    assert manifest["capture_mode"] == "sdk-decorator"
    assert manifest["status"] == "success"
    assert manifest["exit_code"] == 0


def test_agent_capsule_failure_recorded(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capsule"

    @agent(name="failing-agent", version="1.0", capsule_dir=cap_dir)
    def explode() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        explode()

    manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
    assert manifest["status"] == "failure"
    assert manifest["exit_code"] == 1
    assert manifest["error"]["type"] == "ValueError"


def test_agent_capsule_run_id_is_ulid(tmp_path: Path) -> None:
    import re
    cap_dir = tmp_path / "capsule"

    @agent(name="my-agent", version="0.1", capsule_dir=cap_dir)
    def noop() -> None:
        pass

    noop()
    manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
    assert re.match(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$", manifest["run_id"])


def test_agent_capsule_returns_value(tmp_path: Path) -> None:
    cap_dir = tmp_path / "capsule"

    @agent(name="my-agent", version="1.0", capsule_dir=cap_dir)
    def compute() -> int:
        return 42

    assert compute() == 42
