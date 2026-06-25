from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def emit_http(events: list[dict[str, Any]], url: str) -> None:
    import urllib.request

    endpoint = url.rstrip("/") + "/api/v1/lineage"
    for event in events:
        body = json.dumps(event, ensure_ascii=False).encode()
        req = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)


def emit_file(events: list[dict[str, Any]], path: str | Path) -> None:
    with open(path, "a", encoding="utf-8") as f:
        for event in events:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def emit_to(events: list[dict[str, Any]], target: str) -> None:
    if target == "-":
        for event in events:
            sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    elif target.startswith("http://") or target.startswith("https://"):
        emit_http(events, target)
    else:
        emit_file(events, target)


def is_configured() -> bool:
    """Return True if any OpenLineage transport is configured.

    Cheap env-var check intended for hot-path callers that want to skip
    constructing OpenLineage events entirely when no transport is set.
    Mirrors the env-var check inside :func:`emit_if_configured`.
    """
    return bool(
        os.environ.get("OPENLINEAGE_URL", "")
        or os.environ.get("OPENLINEAGE_FILE", "")
    )


def emit_if_configured(events: list[dict[str, Any]]) -> None:
    url = os.environ.get("OPENLINEAGE_URL", "")
    file_path = os.environ.get("OPENLINEAGE_FILE", "")
    if not url and not file_path:
        return
    if url:
        try:
            emit_http(events, url)
        except Exception as exc:
            print(f"[novafabric] OpenLineage HTTP emit failed: {exc}", file=sys.stderr)
    if file_path:
        try:
            emit_file(events, file_path)
        except Exception as exc:
            print(f"[novafabric] OpenLineage file emit failed: {exc}", file=sys.stderr)
