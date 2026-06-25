import json
from pathlib import Path

import pytest

from novafabric.capture.capsule import CapsuleWriter

RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"


def make_writer(tmp_path: Path) -> CapsuleWriter:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=tmp_path)
    w.open()
    return w


def test_writer_creates_staging_dir(tmp_path: Path) -> None:
    make_writer(tmp_path)
    assert (tmp_path / RUN_ID).is_dir()


def test_writer_creates_inputs_outputs(tmp_path: Path) -> None:
    make_writer(tmp_path)
    assert (tmp_path / RUN_ID / "inputs").is_dir()
    assert (tmp_path / RUN_ID / "outputs").is_dir()


def test_writer_creates_empty_jsonl_files(tmp_path: Path) -> None:
    make_writer(tmp_path)
    for fname in ["model-calls.jsonl", "tool-calls.jsonl", "trace.jsonl", "assets.jsonl"]:
        assert (tmp_path / RUN_ID / fname).exists()


def test_capsule_dir_property(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    assert w.capsule_dir == tmp_path / RUN_ID


def test_capsule_dir_raises_before_open() -> None:
    w = CapsuleWriter(run_id=RUN_ID, base_dir=Path("/tmp"))
    with pytest.raises(RuntimeError):
        _ = w.capsule_dir


def test_append_model_call(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    w.append_model_call({"model_call_id": "AAAA", "status": "success"})
    lines = (tmp_path / RUN_ID / "model-calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["model_call_id"] == "AAAA"


def test_append_model_call_multiple(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    for i in range(3):
        w.append_model_call({"model_call_id": str(i)})
    lines = (tmp_path / RUN_ID / "model-calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3


def test_append_tool_call(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    w.append_tool_call({"tool_call_id": "BBBB"})
    lines = (tmp_path / RUN_ID / "tool-calls.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1


def test_append_trace_span(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    w.append_trace_span({"span_id": "aabbccdd11223344", "name": "root"})
    lines = (tmp_path / RUN_ID / "trace.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["name"] == "root"


def test_write_text(tmp_path: Path) -> None:
    w = make_writer(tmp_path)
    w.write_text("capsule.yaml", "schema_version: '0.1.0'\n")
    assert (tmp_path / RUN_ID / "capsule.yaml").read_text() == "schema_version: '0.1.0'\n"


def test_replay_policy_validates(tmp_path: Path) -> None:
    import jsonschema

    schema = json.loads(
        (Path(__file__).parents[1] / "src/novafabric/schemas/replay-policy.schema.json").read_text()
    )
    from novafabric.capture.replay import minimal_replay_policy

    jsonschema.validate(minimal_replay_policy(), schema)
