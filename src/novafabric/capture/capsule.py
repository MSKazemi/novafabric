from __future__ import annotations

import json
import threading
from pathlib import Path

from novafabric.capture.log_level import validate_severity_fields


class CapsuleWriter:
    def __init__(self, run_id: str, base_dir: Path) -> None:
        self._run_id = run_id
        self._base_dir = base_dir
        self._dir: Path | None = None
        self._lock = threading.Lock()

    def open(self) -> None:
        self._dir = self._base_dir / self._run_id
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / "inputs").mkdir(exist_ok=True)
        (self._dir / "outputs").mkdir(exist_ok=True)
        (self._dir / "model-calls.jsonl").touch()
        (self._dir / "tool-calls.jsonl").touch()
        (self._dir / "trace.jsonl").touch()
        (self._dir / "assets.jsonl").touch()

    @property
    def capsule_dir(self) -> Path:
        if self._dir is None:
            raise RuntimeError("CapsuleWriter not opened; call open() first")
        return self._dir

    def _append(self, filename: str, record: dict) -> None:  # type: ignore[type-arg]
        with self._lock:
            with open(self.capsule_dir / filename, "a") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")

    def append_model_call(self, record: dict) -> None:  # type: ignore[type-arg]
        # ADR-0127: reject an out-of-domain severity at write; absent fields
        # pass through untouched (absence is preserved, never back-filled).
        validate_severity_fields(record)
        # ADR-0125: rewrite inline base64 media in the messages to
        # content-addressed MediaPart references (bytes stored under outputs/
        # only when NOVAFABRIC_CAPTURE_MEDIA opts in). Records without inline
        # media are left byte-identical; never raises into the workload.
        try:
            from novafabric.capture.media import annotate_model_call_media

            annotate_model_call_media(record, self.capsule_dir)
        except Exception:  # noqa: BLE001 — capture must never block the workload
            pass
        self._append("model-calls.jsonl", record)

    def append_tool_call(self, record: dict) -> None:  # type: ignore[type-arg]
        validate_severity_fields(record)  # ADR-0127 write-time gate
        # ADR-0128: when the record declares arguments_schema_ref /
        # result_schema_ref, attach a record-only schema_validation verdict.
        # No-op when no schema is declared; never raises into the workload.
        try:
            from novafabric.capture.schema_validation import annotate_tool_call

            annotate_tool_call(record, self.capsule_dir)
        except Exception:  # noqa: BLE001 — capture must never block the workload
            pass
        self._append("tool-calls.jsonl", record)

    def append_trace_span(self, record: dict) -> None:  # type: ignore[type-arg]
        self._append("trace.jsonl", record)

    def write_text(self, filename: str, content: str) -> None:
        with self._lock:
            (self.capsule_dir / filename).write_text(content)

    def write_bytes(self, filename: str, content: bytes) -> None:
        with self._lock:
            (self.capsule_dir / filename).write_bytes(content)

    def append_tool_permission_event(self, event_dict: dict) -> None:  # type: ignore[type-arg]
        """Append a ToolPermissionEvent to the DSSE signing scope.

        cap-004: ToolPermissionEvent entities are included in the DSSE signing
        scope so that post-hoc tampering of a permission decision is detectable.
        They are written to tool-permission-events.jsonl in the capsule directory.
        """
        self._append("tool-permission-events.jsonl", event_dict)
