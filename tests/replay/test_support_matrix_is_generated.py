"""ADR-0304: the replay support matrix in the docs is generated, and every claim in
it is held to the code and the tests.

Three ways the public matrix could lie, each caught here:

1. the page drifts from the rows (someone edits the Markdown by hand);
2. a row says "served"/"refused" for an SDK method the dispatcher does not patch
   that way -- or the dispatcher patches a method no row speaks for;
3. a row cites an evidence test that does not exist (renamed or deleted).
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from novafabric.replay._contract import TOOL_SURFACE_MCP
from novafabric.replay._dispatcher import SERVED_MODEL_SURFACES, UNSUPPORTED_MODEL_SURFACES
from novafabric.replay._support_matrix import BEGIN_MARKER, END_MARKER, ROWS, render_block

REPO = Path(__file__).resolve().parents[2]
PAGE = REPO / "docs" / "architecture" / "replay-modes.md"


def _generator():  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(
        "gen_replay_support_matrix", REPO / "scripts" / "gen_replay_support_matrix.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_docs_page_carries_the_generated_matrix() -> None:
    text = PAGE.read_text(encoding="utf-8")
    assert text.count(BEGIN_MARKER) == 1 and text.count(END_MARKER) == 1
    assert render_block() in text, (
        "docs/architecture/replay-modes.md is stale: run "
        "`uv run python scripts/gen_replay_support_matrix.py`"
    )
    assert _generator().updated_page(text) == text


def test_rows_match_the_dispatcher_patch_tables() -> None:
    served = {(m, c, a) for _, m, c, a, _ in SERVED_MODEL_SURFACES}
    served.add(("mcp.client.session", "ClientSession", "call_tool"))
    refused = {(m, c, a) for _, m, c, a, _ in UNSUPPORTED_MODEL_SURFACES}
    claimed_served = {p for r in ROWS if r.replay == "served" for p in r.patches}
    claimed_refused = {p for r in ROWS if r.replay == "refused" for p in r.patches}
    assert claimed_served == served
    assert claimed_refused == refused
    # Rows that say nothing is patched must not claim a served/refused SDK method.
    for row in ROWS:
        if row.replay in ("not intercepted", "capture only"):
            assert not row.patches, row.surface
            assert not row.servers, row.surface
    assert TOOL_SURFACE_MCP == "mcp.ClientSession.call_tool"


def test_registered_server_rows_match_what_the_dispatcher_registers() -> None:
    """ADR-0306: a surface served by a registered server (not a patch) is claimed
    by exactly the rows that say so -- checked against a real install."""
    from novafabric.capture import record
    from novafabric.replay._contract import TOOL_SURFACE_MCP, TOOL_SURFACE_PYTHON, ReplayEventLog
    from novafabric.replay._dispatcher import MockToolDispatcher

    dispatcher = MockToolDispatcher([], events=ReplayEventLog(None))
    dispatcher.install()
    try:
        registered = set(dispatcher.installed_surfaces) - {TOOL_SURFACE_MCP}
        assert record._get_tool_handler() is not None
    finally:
        dispatcher.uninstall()
    assert record._get_tool_handler() is None
    claimed = {s for r in ROWS if r.replay == "served" for s in r.servers}
    assert claimed == registered == {TOOL_SURFACE_PYTHON}
    for row in ROWS:
        if row.servers:
            assert row.replay == "served" and not row.patches, row.surface


def test_every_cited_evidence_test_exists() -> None:
    cited = [e for row in ROWS for e in row.evidence if "::" in e]
    assert cited, "the matrix cites no tests at all"
    for entry in cited:
        path, name = entry.split("::", 1)
        source = (REPO / path).read_text(encoding="utf-8")
        assert re.search(rf"^def {re.escape(name)}\(", source, re.MULTILINE), entry


def test_every_served_row_cites_a_test() -> None:
    for row in ROWS:
        if row.replay == "served":
            assert any("::" in e for e in row.evidence), row.surface
