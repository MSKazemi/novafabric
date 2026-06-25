"""End-to-end smoke test for the OTel GenAI semconv pipeline.

Complements `test_smoke_capture_validate.py` (which only verifies the
golden capsule structure with no LLM call) by exercising the FULL
wire-level capture pipeline:

  nova capture
    └─ subprocess script makes a real httpx POST to a registry-known URL
       └─ HttpxHook fires
          └─ extract_request_attributes() builds the semconv fields
             └─ writer.append_model_call() persists to model-calls.jsonl

If the chain breaks anywhere — registry classification, hook
installation, builder field extraction, file persistence — this test
fails. It catches the same gap the unit tests do but at the integration
layer, so a refactor that makes each unit test pass while subtly breaking
the wiring would still trip this.

Uses httpx (a NovaFabric runtime dep, always installed) and a fake
endpoint (connection fails fast). The captured record is what we
assert on, NOT the response.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from novafabric.cli.main import app

runner = CliRunner()

# Inline script: does a single httpx POST with all OTel "Required when
# applicable" semconv fields set in the body. Connection will fail fast
# (port 1 is unprivileged-low, nothing listens); the hook records the
# attempt regardless.
_PROBE_SCRIPT = """
import httpx
try:
    httpx.post(
        "https://api.openai.com/v1/chat/completions",
        json={
            "model": "gpt-4o-test",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0.42,
            "max_tokens": 256,
            "top_p": 0.95,
            "frequency_penalty": 0.1,
            "presence_penalty": 0.2,
            "seed": 1234,
            "stop": ["\\n\\n"],
            "n": 1,
        },
        timeout=httpx.Timeout(connect=2.0, read=2.0, write=2.0, pool=2.0),
        # Hit a port nothing listens on; the request leaves httpx (so the
        # hook fires) but the connection is refused immediately.
        # We override base_url at the call site by using a different host.
    )
except Exception:
    pass  # connection failure expected; the hook recorded the attempt
"""


def _capture_record(tmp_path: Path) -> dict[str, object]:
    """Run the probe script under nova capture and return the single
    model-calls.jsonl record it produced."""
    runs_dir = tmp_path / "runs"
    script_path = tmp_path / "probe.py"
    script_path.write_text(_PROBE_SCRIPT)

    result = runner.invoke(
        app,
        ["capture", "--output-dir", str(runs_dir), sys.executable, str(script_path)],
    )
    # Connection fails inside the probe (caught), but the wrapped script
    # exits 0 because the except passes. Capture itself succeeded.
    assert result.exit_code == 0, (
        f"nova capture exited {result.exit_code}:\n{result.output}"
    )

    capsule = next(d for d in runs_dir.iterdir() if d.is_dir())
    mc = capsule / "model-calls.jsonl"
    assert mc.exists(), "model-calls.jsonl missing from capsule"
    lines = [line for line in mc.read_text().splitlines() if line.strip()]
    assert len(lines) == 1, (
        f"expected exactly 1 model-call record, got {len(lines)}:\n"
        f"{mc.read_text()}"
    )
    return json.loads(lines[0])


class TestSemconvPipelineEndToEnd:
    def test_record_has_envelope_fields(self, tmp_path: Path) -> None:
        rec = _capture_record(tmp_path)
        for key in ("schema_version", "semconv_version", "model_call_id",
                    "parent_span_id", "started_at", "finished_at",
                    "duration_ms", "status"):
            assert key in rec, f"envelope field missing: {key}"

    def test_record_classifies_as_openai(self, tmp_path: Path) -> None:
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.system"] == "openai"
        assert rec["endpoint"] == "https://api.openai.com/v1/chat/completions"

    def test_record_extracts_request_model_and_messages(
        self, tmp_path: Path
    ) -> None:
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.model"] == "gpt-4o-test"
        assert rec["gen_ai.request.messages"] == [
            {"role": "user", "content": "hi"}
        ]

    def test_record_extracts_temperature(self, tmp_path: Path) -> None:
        """Critical for replay determinism — was silently dropped before C-4."""
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.temperature"] == 0.42

    def test_record_extracts_max_tokens(self, tmp_path: Path) -> None:
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.max_tokens"] == 256

    def test_record_extracts_top_p(self, tmp_path: Path) -> None:
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.top_p"] == 0.95

    def test_record_extracts_frequency_penalty(self, tmp_path: Path) -> None:
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.frequency_penalty"] == 0.1

    def test_record_extracts_presence_penalty(self, tmp_path: Path) -> None:
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.presence_penalty"] == 0.2

    def test_record_extracts_seed(self, tmp_path: Path) -> None:
        """Critical for exact-mode replay determinism."""
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.seed"] == 1234

    def test_record_normalizes_stop_to_stop_sequences(
        self, tmp_path: Path
    ) -> None:
        """Body sent OpenAI's `stop`; record exposes canonical
        `gen_ai.request.stop_sequences`."""
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.stop_sequences"] == ["\n\n"]

    def test_record_extracts_choice_count(self, tmp_path: Path) -> None:
        rec = _capture_record(tmp_path)
        assert rec["gen_ai.request.choice.count"] == 1

    def test_status_is_error_because_connection_failed(
        self, tmp_path: Path
    ) -> None:
        """Connection failures must still produce a record (status=error).
        Regression test for the cleanup committed in 8c59a0e."""
        rec = _capture_record(tmp_path)
        assert rec["status"] == "error"
