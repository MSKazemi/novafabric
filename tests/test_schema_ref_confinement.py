"""Re-validating a stored capsule never reads a schema from outside it (ADR-0128 amendment).

ADR-0128 lets a ``*_schema_ref`` be an absolute local path, which is right at
capture: the application that declares the ref is the one being recorded. But
``nova replay`` (``revalidate_tool_calls``) and ``nova validate --tool-schemas``
re-validate a capsule someone *handed* you. An absolute ref there made the
replay read any JSON file the user can read, and the verdict (valid, invalid,
unresolved) leaked whether it exists and what shape it has. A relative ref that
went through a link in the capsule did the same.

Now re-validation resolves a ref only inside the capsule (no ``..``, no link,
no FIFO, at most ``MAX_SCHEMA_BYTES``) or inside NovaFabric's own packaged
``schemas/`` directory. Anything else is a ``null`` verdict with one
``schema-unresolved`` error, never a violation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import novafabric
from novafabric.capture.schema_validation import (
    annotate_tool_call,
    revalidate_tool_calls,
    validate_capsule_tool_calls,
)

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX links and FIFOs")

#: A schema the payload below violates: if it is read, the verdict is a violation.
_REJECTING = {"type": "integer"}


def _record(ref: str) -> dict[str, Any]:
    return {
        "tool_call_id": "tc-1",
        "tool_name": "web_search",
        "arguments": {"query": "hello"},
        "arguments_schema_ref": ref,
        "status": "success",
    }


@pytest.fixture
def capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "cap"
    (cap / "extensions" / "io.test").mkdir(parents=True)
    return cap


@pytest.fixture
def outside_schema(tmp_path: Path) -> Path:
    path = tmp_path / "outside" / "any.json"
    path.parent.mkdir()
    path.write_text(json.dumps(_REJECTING))
    return path


def _unresolved(capsule: Path, ref: str) -> None:
    record = _record(ref)
    assert revalidate_tool_calls([record], capsule) == []
    (capsule / "tool-calls.jsonl").write_text(json.dumps(record) + "\n")
    report = validate_capsule_tool_calls(capsule)
    assert report.violations == []
    assert report.unresolved == 1
    [verdict] = report.verdicts
    assert verdict is not None
    assert verdict["arguments_valid"] is None
    assert [e["keyword"] for e in verdict["errors"]] == ["schema-unresolved"]


def test_an_absolute_ref_is_never_read_when_revalidating(
    capsule: Path, outside_schema: Path
) -> None:
    _unresolved(capsule, str(outside_schema))


def test_a_relative_ref_through_a_linked_file_is_never_read(
    capsule: Path, outside_schema: Path
) -> None:
    (capsule / "extensions" / "io.test" / "args.schema.json").symlink_to(outside_schema)
    _unresolved(capsule, "extensions/io.test/args.schema.json")


def test_a_relative_ref_through_a_linked_directory_is_never_read(
    capsule: Path, outside_schema: Path
) -> None:
    (capsule / "extensions" / "linked").symlink_to(outside_schema.parent)
    _unresolved(capsule, "extensions/linked/any.json")


@pytest.mark.timeout(30)
def test_a_fifo_schema_is_unresolved_without_blocking(capsule: Path) -> None:
    os.mkfifo(capsule / "extensions" / "io.test" / "args.schema.json")
    _unresolved(capsule, "extensions/io.test/args.schema.json")


def test_a_dev_zero_link_is_unresolved(capsule: Path) -> None:
    (capsule / "extensions" / "io.test" / "args.schema.json").symlink_to("/dev/zero")
    _unresolved(capsule, "extensions/io.test/args.schema.json")


@pytest.mark.parametrize("ref", ["../outside/any.json", "extensions/../../outside/any.json"])
def test_a_dotdot_ref_is_unresolved(capsule: Path, outside_schema: Path, ref: str) -> None:
    _unresolved(capsule, ref)


def test_a_schema_inside_the_capsule_is_still_checked(capsule: Path) -> None:
    (capsule / "extensions" / "io.test" / "args.schema.json").write_text(json.dumps(_REJECTING))
    [finding] = revalidate_tool_calls(
        [_record("extensions/io.test/args.schema.json")], capsule
    )
    assert finding["arguments_valid"] is False


def test_a_packaged_schema_is_still_checked(capsule: Path) -> None:
    packaged = Path(novafabric.__file__).parent / "schemas" / "tool-call.schema.json"
    assert packaged.is_file()
    [finding] = revalidate_tool_calls([_record(str(packaged))], capsule)
    assert finding["arguments_valid"] is False  # {"query": ...} is not a tool-call record


def test_capture_still_honours_an_absolute_ref(capsule: Path, outside_schema: Path) -> None:
    """At capture the application declares its own schema: ADR-0128 keeps absolute paths."""
    record = _record(str(outside_schema))
    annotate_tool_call(record, capsule)
    assert record["schema_validation"]["arguments_valid"] is False
