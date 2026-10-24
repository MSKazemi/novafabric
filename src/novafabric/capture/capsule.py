from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from novafabric._capsule_fs import open_capsule_file_for_write, replace_capsule_file
from novafabric.capture._jsonable import json_default, to_jsonable
from novafabric.capture._tool_scope import mark_nested
from novafabric.capture.log_level import validate_severity_fields

_SEPARATORS = (",", ":")


def _dumps(record: dict) -> str:  # type: ignore[type-arg]
    """One JSONL line for *record*.

    A record of plain JSON values serialises byte-identically to before. A
    value the hooks took from the caller (an SDK pydantic object, a datetime)
    is converted by :func:`json_default` instead of raising ``TypeError``; a
    shape ``default`` cannot reach (a non-string dict key, a reference cycle)
    falls back to a full :func:`to_jsonable` copy.
    """
    try:
        return json.dumps(record, separators=_SEPARATORS, default=json_default)
    except (TypeError, ValueError, RecursionError):
        return json.dumps(to_jsonable(record), separators=_SEPARATORS)


class CapsuleWriter:
    """Writes one capsule's files, and never follows a link while doing so.

    Under ``nova capture`` the workload writes into its own capsule, and with a
    container runner the capsule directory is bind-mounted into the container.
    So every write goes through :mod:`novafabric._capsule_fs`: an append opens
    the stream ``O_NOFOLLOW | O_NONBLOCK`` and refuses a link or a FIFO
    (``CapsuleEntryError``), and a whole-file write replaces the entry by an
    atomic rename, so a link, FIFO or hard link planted at the name is replaced,
    never written through. A writer added later inherits this by default.
    """

    def __init__(self, run_id: str, base_dir: Path) -> None:
        # Guard against an empty/blank run_id: ``base_dir / "" == base_dir`` would
        # write capsule files (model-calls.jsonl, tool-calls.jsonl, …) directly
        # into the capsule store root instead of an isolated ``<run_id>/`` subdir,
        # corrupting the store and breaking run-detail resolution.
        if not run_id or not run_id.strip() or "/" in run_id or run_id in {".", ".."}:
            raise ValueError(f"CapsuleWriter requires a valid run_id, got: {run_id!r}")
        self._run_id = run_id
        self._base_dir = base_dir
        self._dir: Path | None = None
        self._root_identity: tuple[int, int] | None = None
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
        st = os.lstat(self._dir)
        self._root_identity = (st.st_dev, st.st_ino)

    @property
    def root_identity(self) -> tuple[int, int] | None:
        """``(st_dev, st_ino)`` of the capsule directory as :meth:`open` created it.

        Lets capture finalisation tell that the directory it is about to write
        into is still the one it created, not a link the workload put in its
        place.
        """
        return self._root_identity

    @property
    def capsule_dir(self) -> Path:
        if self._dir is None:
            raise RuntimeError("CapsuleWriter not opened; call open() first")
        return self._dir

    def _append(self, filename: str, record: dict) -> None:  # type: ignore[type-arg]
        line = _dumps(record)
        with self._lock:
            with open_capsule_file_for_write(
                self.capsule_dir, filename, mode="a", operation="the capsule writer"
            ) as f:
                f.write((line + "\n").encode("utf-8"))

    def append_model_call(self, record: dict) -> None:  # type: ignore[type-arg]
        # ADR-0127: reject an out-of-domain severity at write; absent fields
        # pass through untouched (absence is preserved, never back-filled).
        validate_severity_fields(record)
        # ADR-0306 D3: inside a record.tool boundary, mark the record as nested.
        mark_nested(record)
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
        mark_nested(record)  # ADR-0306 D3: inside a record.tool boundary
        # ADR-0128: when the record declares arguments_schema_ref /
        # result_schema_ref, attach a record-only schema_validation verdict.
        # No-op when no schema is declared; never raises into the workload.
        try:
            from novafabric.capture.schema_validation import annotate_tool_call

            annotate_tool_call(record, self.capsule_dir)
        except Exception:  # noqa: BLE001 — capture must never block the workload
            pass
        self._append("tool-calls.jsonl", record)

    def append_jsonl(self, filename: str, record: dict) -> None:  # type: ignore[type-arg]
        """Append *record* to a capsule-root JSONL stream of the caller's own.

        For a vendor stream an adapter points at from manifest ``extensions``
        (e.g. ``bedrock-traces.jsonl``); the standard streams have their own
        methods. Opened like every capsule write: no link, no special file.
        """
        self._append(filename, record)

    def append_trace_span(self, record: dict) -> None:  # type: ignore[type-arg]
        self._append("trace.jsonl", record)

    def write_text(self, filename: str, content: str) -> None:
        """Replace capsule file *filename* (capsule-relative) with UTF-8 *content*."""
        self.write_bytes(filename, content.encode("utf-8"))

    def write_bytes(self, filename: str, content: bytes) -> None:
        """Replace capsule file *filename* (capsule-relative) with *content*.

        An atomic rename that never follows a link planted at *filename* or in
        a parent directory (see the class docstring).
        """
        with self._lock:
            replace_capsule_file(
                self.capsule_dir, filename, content, operation="the capsule writer"
            )

    def append_tool_permission_event(self, event_dict: dict) -> None:  # type: ignore[type-arg]
        """Append a ToolPermissionEvent to the DSSE signing scope.

        cap-004: ToolPermissionEvent entities are included in the DSSE signing
        scope so that post-hoc tampering of a permission decision is detectable.
        They are written to tool-permission-events.jsonl in the capsule directory.
        """
        self._append("tool-permission-events.jsonl", event_dict)
