import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

from novafabric.capture.env import capture_environment

SCHEMA = json.loads(
    (Path(__file__).parents[1] / "src/novafabric/schemas/environment.schema.json").read_text()
)

NOW = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def test_env_lock_validates_against_schema() -> None:
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    jsonschema.validate(lock, SCHEMA, format_checker=jsonschema.FormatChecker())


def test_env_lock_required_fields() -> None:
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    assert lock["schema_version"] == "0.1.0"
    assert lock["mode"] in ("deterministic", "best-effort")
    assert "host" in lock
    assert "runtime" in lock
    assert "python" in lock
    assert "hardware" in lock
    assert "runtime_env" in lock
    assert "locale" in lock
    assert "captured_at" in lock
    assert "captured_by" in lock


def test_env_lock_hardware_gpus_list() -> None:
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    assert isinstance(lock["hardware"]["gpus"], list)


def test_env_lock_python_section() -> None:
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    py = lock["python"]
    assert py["interpreter"] in ("cpython", "pypy3")
    assert py["package_manager"] in ("uv", "poetry", "pdm", "pip", "none")
    valid_kinds = ("uv.lock", "poetry.lock", "pdm.lock", "requirements.txt", "none")
    assert py["lock_file_kind"] in valid_kinds


def test_env_lock_redacts_hostname() -> None:
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    raw_hostname = socket.gethostname()
    hostname_field = lock["host"].get("hostname", "")
    assert raw_hostname not in hostname_field, "raw hostname must be redacted"


def test_env_lock_best_effort_has_reasons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    if lock["mode"] == "best-effort":
        assert len(lock["best_effort_reasons"]) >= 1
        assert lock["best_effort_reasons"][0]["kind"] in (
            "missing_lock_file", "container_digest_unresolved",
            "driver_version_unavailable", "no_gpu_passthrough", "other",
        )


def test_env_lock_runtime_env_structure() -> None:
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    rt = lock["runtime_env"]
    assert "policy" in rt
    assert "allow_list" in rt
    assert "deny_list" in rt
    assert "recorded" in rt
    assert isinstance(rt["dropped_count"], int)


# gap-007 (SOTA sweep src-402/403/416): LLM inference numerical-determinism facet.
# Captures the signal that separates environmental drift from genuine regression
# during replay/diff. Populated best-effort from the NOVAFABRIC_INFERENCE_* contract.


def test_env_lock_inference_facet_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_INFERENCE_ENGINE", "vllm")
    monkeypatch.setenv("NOVAFABRIC_INFERENCE_TP_SIZE", "8")
    monkeypatch.setenv("NOVAFABRIC_INFERENCE_DTYPE", "bfloat16")
    monkeypatch.setenv("NOVAFABRIC_INFERENCE_BATCH_SIZE", "32")
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    inf = lock["hardware"]["inference"]
    assert inf["engine"] == "vllm"
    assert inf["tensor_parallel_size"] == 8
    assert inf["dtype"] == "bfloat16"
    assert inf["batch_size"] == 32
    jsonschema.validate(lock, SCHEMA, format_checker=jsonschema.FormatChecker())


def test_env_lock_inference_absent_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "NOVAFABRIC_INFERENCE_ENGINE",
        "NOVAFABRIC_INFERENCE_TP_SIZE",
        "NOVAFABRIC_INFERENCE_DTYPE",
        "NOVAFABRIC_INFERENCE_BATCH_SIZE",
    ):
        monkeypatch.delenv(key, raising=False)
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    assert "inference" not in lock["hardware"]
    jsonschema.validate(lock, SCHEMA, format_checker=jsonschema.FormatChecker())


def test_env_lock_inference_invalid_int_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_INFERENCE_ENGINE", "tgi")
    monkeypatch.setenv("NOVAFABRIC_INFERENCE_TP_SIZE", "not-an-int")
    lock = capture_environment(created_at=NOW, run_id=RUN_ID)
    inf = lock["hardware"].get("inference", {})
    assert inf.get("engine") == "tgi"
    assert "tensor_parallel_size" not in inf  # bad int degrades, never crashes
    jsonschema.validate(lock, SCHEMA, format_checker=jsonschema.FormatChecker())
