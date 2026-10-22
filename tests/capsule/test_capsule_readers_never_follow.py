"""Capsule *readers* never follow a link and never block on a special file.

``test_capsule_links_never_followed.py`` pins the exporters that copy a whole
capsule. The second adversarial review found the readers that parse one
well-known file: ``nova export --html`` put the target of a linked
``model-calls.jsonl`` into the page four times and hung on a FIFO
``tool-calls.jsonl``; the knowledge-graph consumer ingested a linked stream; the
cost and suggestion scans read a sibling's linked stream.

Each reader below gets the same five shapes:

* ``file-link``: the stream it reads is a link to a file outside the capsule
  (the canary must not appear in its output);
* ``fifo``: the stream is a FIFO (the reader must not block);
* ``dev-zero``: the stream is a link to ``/dev/zero`` (the reader must not
  read forever; the test caps the address space so a regression fails instead
  of taking the machine down);
* ``dir-link``: the directory the reader walks into is a link to a directory
  outside the capsule (for a scan: a sibling *capsule entry* is such a link);
* ``root-link``: the capsule is named through a link (``runs/latest``). A
  command you point at a capsule still follows that one link, as documented.
  A scan never lists a linked entry.

A reader may refuse (``CapsuleEntryError``, or a CLI exit code of 1 or 2) or
carry on without the file. It may not leak, hang or crash.
"""

from __future__ import annotations

import asyncio
import json
import os
import resource
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from novafabric._capsule_fs import CapsuleEntryError

pytestmark = [
    pytest.mark.skipif(sys.platform != "linux", reason="POSIX links, FIFOs and /dev/zero"),
    pytest.mark.timeout(60),
]

CANARY = "CANARY-READER-91c2"
RUN = "01J9ZZREADERSNOFOLLOW00001"
REAL_MODEL = "gpt-real-model"


def _record(model: str, *, tool: str = "real_tool") -> dict[str, object]:
    return {
        "model_call_id": f"mc-{model}",
        "tool_call_id": f"tc-{model}",
        "tool_name": tool,
        "gen_ai.system": "openai",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.usage.input_tokens": 3,
        "gen_ai.usage.output_tokens": 4,
        "started_at": "2099-01-01T00:00:00Z",
        "status": "ok",
        "source_run_id": RUN,
        "target_run_id": RUN,
        "edge_type": "contains",
    }


def _write_capsule(cap: Path, run_id: str = RUN, model: str = REAL_MODEL) -> Path:
    (cap / "inputs").mkdir(parents=True)
    (cap / "outputs").mkdir()
    manifest = {
        "run_id": run_id,
        "status": "success",
        "exit_code": 0,
        "created_at": "2099-01-01T00:00:00Z",
        "finished_at": "2099-01-01T00:00:01Z",
        "capture_mode": "cli-wrapper",
        "command": ["python", "agent.py"],
        "model_call_count": 1,
        "tool_call_count": 2,
    }
    (cap / "capsule.yaml").write_text(yaml.safe_dump(manifest))
    line = json.dumps(_record(model)) + "\n"
    for name in ("model-calls.jsonl", "events.jsonl", "trace.jsonl", "lineage.jsonl"):
        (cap / name).write_text(line)
    (cap / "tool-calls.jsonl").write_text(line + line)
    (cap / "assets.jsonl").write_text("")
    return cap


@dataclass
class Reader:
    """How to run one reader against a capsule laid out under ``base``."""

    run: Callable[[Path, Path], str]
    #: The capsule file the attack replaces.
    target: str = "model-calls.jsonl"
    #: True for a reader that scans every capsule under a base directory.
    scan: bool = False
    #: Named errors this reader may raise to refuse (besides CapsuleEntryError).
    refusals: tuple[type[Exception], ...] = ()
    #: Text a successful read of the clean capsule puts in the output.
    marker: str = REAL_MODEL


# -- readers --------------------------------------------------------------------


def _viewer(cap: Path, work: Path) -> str:
    from novafabric.viewer.html import export_capsule_html

    out, warnings = export_capsule_html(cap, work / "view.html")
    return out.read_text() + "\n".join(warnings)


def _viewer_cli(cap: Path, work: Path) -> str:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    result = CliRunner().invoke(app, ["export", "--html", str(cap), "-o", str(work / "v.html")])
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        raise result.exception
    assert result.exit_code in (0, 1, 2), result.output
    html = (work / "v.html").read_text() if (work / "v.html").exists() else ""
    return result.output + html


def _kg_consumer(cap: Path, work: Path) -> str:
    from novafabric.kg.consumer import CapsuleEventConsumer

    async def collect() -> list[dict[str, object]]:
        return [ev async for ev in CapsuleEventConsumer().iter_from_directory(cap.parent)]

    return json.dumps(asyncio.run(collect()))


def _logical_model_calls(cap: Path, work: Path) -> str:
    from novafabric.capture.record_roles import read_logical_model_calls

    return json.dumps(read_logical_model_calls(cap / "model-calls.jsonl"))


def _suggestions(cap: Path, work: Path) -> str:
    from novafabric.registry.suggestion_engine import SuggestionEngine

    found = SuggestionEngine().analyze_recent(cap.parent, limit=50)
    return json.dumps([s.__dict__ for s in found], default=str)


def _serve_cost_aggregate(cap: Path, work: Path) -> str:
    from novafabric.serve.app import _cost_report_from_capsules

    return json.dumps(_cost_report_from_capsules(cap.parent, 36500), default=str)


def _diagnose(cap: Path, work: Path) -> str:
    from novafabric.diagnose.attribution import attribute_failure

    return repr(attribute_failure(cap))


def _diff(cap: Path, work: Path) -> str:
    from novafabric.diff._engine import DiffEngine

    other = _write_capsule(work / "other", run_id="01J9ZZREADERSNOFOLLOW0000B", model="m-b")
    return repr(DiffEngine().compare(cap, other))


def _replay_reads(cap: Path, work: Path) -> str:
    from novafabric.replay._engine import _load_capsule, _read_jsonl

    return json.dumps([_load_capsule(cap), _read_jsonl(cap / "model-calls.jsonl")], default=str)


def _assure(cap: Path, work: Path) -> str:
    from novafabric.assure.checker import AssuranceChecker

    return repr(AssuranceChecker().check_all(cap))


def _content_index(cap: Path, work: Path) -> str:
    from novafabric.query.content_index import extract_docs

    docs, _ = extract_docs(cap, RUN)
    return repr(docs)


def _query_index(cap: Path, work: Path) -> str:
    from novafabric.query.indexer import scan_capsule_dir

    return repr(scan_capsule_dir(cap.parent))


def _lineage_facets(cap: Path, work: Path) -> str:
    from novafabric.lineage._openlineage import build_events_from_capsule

    return json.dumps(build_events_from_capsule(cap, with_facets=True), default=str)


def _annex_iv(cap: Path, work: Path) -> str:
    from novafabric.compliance.export.annex_iv import AnnexIVExporter

    doc = AnnexIVExporter().build_annex_iv_document(deployment_id="d", capsule_dir=cap)
    return json.dumps(doc, default=str)


def _agent_graph(cap: Path, work: Path) -> str:
    from novafabric.agent_graph.builder import build_agent_graph

    return repr(build_agent_graph(cap))


def _query_error() -> type[Exception]:
    from novafabric.query.errors import QueryIndexError

    return QueryIndexError


def _diff_error() -> type[Exception]:
    from novafabric.diff._engine import CapsuleFileError

    return CapsuleFileError


READERS: dict[str, Reader] = {
    "diagnose": Reader(
        _diagnose, target="capsule.yaml", refusals=(FileNotFoundError,), marker=RUN
    ),
    "diff": Reader(_diff, refusals=(_diff_error(),)),
    "replay": Reader(_replay_reads),
    "assure": Reader(_assure, target="capsule.yaml", marker=RUN),
    "content-index": Reader(_content_index, target="tool-calls.jsonl", marker="real"),
    "query-index": Reader(_query_index, scan=True, refusals=(_query_error(),)),
    "openlineage": Reader(_lineage_facets, target="capsule.yaml", marker=RUN),
    "annex-iv": Reader(_annex_iv, target="capsule.yaml", marker=RUN),  # refuses
    "agent-graph": Reader(_agent_graph, target="tool-calls.jsonl", marker="real_tool"),
    "export-html": Reader(_viewer),
    "export-html-cli": Reader(_viewer_cli, target="tool-calls.jsonl"),
    "kg-consumer": Reader(_kg_consumer, scan=True),
    "record-roles": Reader(_logical_model_calls),
    "serve-cost-scan": Reader(_serve_cost_aggregate, scan=True),
    "suggest-register": Reader(_suggestions, scan=True),
}


# -- shapes ---------------------------------------------------------------------


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    path = outside / "secret.jsonl"
    # Parses as JSON, JSONL and YAML, so whichever file it stands in for, a
    # reader that follows the link surfaces the canary.
    path.write_text(json.dumps({**_record(CANARY, tool=CANARY), "run_id": CANARY}) + "\n")
    return path


@pytest.fixture
def memory_cap() -> Iterator[None]:
    """Cap the address space so an unbounded read fails with MemoryError, not OOM."""
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    pages = int(Path("/proc/self/statm").read_text().split()[0])
    cap = pages * os.sysconf("SC_PAGE_SIZE") + 1_500_000_000
    if hard != resource.RLIM_INFINITY:
        cap = min(cap, hard)
    resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_AS, (soft, hard))


def _replace(path: Path) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()


def _plant(shape: str, reader: Reader, base: Path, cap: Path, secret: Path) -> Path:
    """Plant *shape*; return the capsule path to hand the reader."""
    target = cap / reader.target
    if shape == "file-link":
        _replace(target)
        target.symlink_to(secret)
    elif shape == "fifo":
        _replace(target)
        os.mkfifo(target)
    elif shape == "dev-zero":
        _replace(target)
        target.symlink_to("/dev/zero")
    elif shape == "dir-link":
        outside = secret.parent / "capsule-elsewhere"
        _write_capsule(outside, run_id="01J9ZZREADERSNOFOLLOW0000X", model=CANARY)
        (outside / reader.target).write_text(secret.read_text() * 2)
        (base / "01J9ZZREADERSNOFOLLOW0000X").symlink_to(outside, target_is_directory=True)
    elif shape == "root-link":
        (base.parent / "latest").symlink_to(cap, target_is_directory=True)
        return base.parent / "latest"
    return cap


SHAPES = ("file-link", "fifo", "dev-zero", "dir-link", "root-link")


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("name", sorted(READERS))
def test_reader_never_follows_a_link_or_blocks(
    name: str, shape: str, tmp_path: Path, secret: Path, memory_cap: None
) -> None:
    reader = READERS[name]
    base = tmp_path / "runs" / "capsules"
    cap = _write_capsule(base / RUN)
    named = _plant(shape, reader, base, cap, secret)
    work = tmp_path / "work"
    work.mkdir()
    try:
        output = reader.run(named, work)
    except (CapsuleEntryError, *reader.refusals) as exc:
        assert CANARY not in str(exc)
        return  # refused, with a named error: allowed
    assert CANARY not in output, f"{name} followed a link ({shape})"
    if shape == "root-link":
        # A capsule named through a link is still read: the link is the one you gave.
        assert reader.marker in output or reader.scan, f"{name} no longer reads runs/latest"


@pytest.mark.parametrize("name", sorted(READERS))
def test_reader_still_reads_a_clean_capsule(name: str, tmp_path: Path) -> None:
    reader = READERS[name]
    base = tmp_path / "runs" / "capsules"
    cap = _write_capsule(base / RUN)
    work = tmp_path / "work"
    work.mkdir()
    output = reader.run(cap, work)
    assert reader.marker in output or "real_tool" in output, f"{name} read nothing"


def test_export_html_names_the_skipped_section(tmp_path: Path, secret: Path) -> None:
    from novafabric.viewer.html import export_capsule_html

    cap = _write_capsule(tmp_path / RUN)
    (cap / "model-calls.jsonl").unlink()
    (cap / "model-calls.jsonl").symlink_to(secret)
    _, warnings = export_capsule_html(cap, tmp_path / "v.html")
    assert any("model-calls.jsonl" in w and "symlink" in w for w in warnings), warnings


def test_export_html_refuses_a_linked_manifest(tmp_path: Path, secret: Path) -> None:
    from typer.testing import CliRunner

    from novafabric.cli.main import app

    cap = _write_capsule(tmp_path / RUN)
    (cap / "capsule.yaml").unlink()
    (cap / "capsule.yaml").symlink_to(secret)
    out = tmp_path / "v.html"
    result = CliRunner().invoke(app, ["export", "--html", str(cap), "-o", str(out)])
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 2, result.output
    assert not out.exists()
    assert CANARY not in result.output


def test_kg_consumer_skips_a_linked_zip(tmp_path: Path, secret: Path) -> None:
    import zipfile

    from novafabric.kg.consumer import CapsuleEventConsumer

    outside = secret.parent / "cap.zip"
    with zipfile.ZipFile(outside, "w") as zf:
        zf.writestr("x/model-calls.jsonl", secret.read_text())
    base = tmp_path / "base"
    base.mkdir()
    (base / "linked.zip").symlink_to(outside)

    async def collect() -> list[dict[str, object]]:
        return [ev async for ev in CapsuleEventConsumer().iter_from_directory(base)]

    assert CANARY not in json.dumps(asyncio.run(collect()))



# -- the shared helpers ------------------------------------------------------------


def test_iter_capsule_dirs_lists_only_real_directories(tmp_path: Path, secret: Path) -> None:
    from novafabric._capsule_fs import iter_capsule_dirs

    base = tmp_path / "caps"
    real = _write_capsule(base / RUN)
    (base / "latest").symlink_to(real, target_is_directory=True)  # alias inside
    (base / "outside").symlink_to(secret.parent, target_is_directory=True)
    linked_manifest = base / "linked-manifest"
    linked_manifest.mkdir()
    (linked_manifest / "capsule.yaml").symlink_to(secret)
    (base / "fifo").mkdir()
    os.mkfifo(base / "fifo" / "capsule.yaml")
    assert iter_capsule_dirs(base) == [real]


def test_read_capsule_file_max_bytes_refuses_instead_of_truncating(tmp_path: Path) -> None:
    from novafabric._capsule_fs import CapsuleFileTooLargeError, read_capsule_file

    cap = _write_capsule(tmp_path / RUN)
    (cap / "big.json").write_bytes(b"x" * 11)
    assert read_capsule_file(cap, "big.json", max_bytes=11) == b"x" * 11
    with pytest.raises(CapsuleFileTooLargeError):
        read_capsule_file(cap, "big.json", max_bytes=10)


def test_read_capsule_text_or_none_logs_a_refusal(
    tmp_path: Path, secret: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from novafabric._capsule_fs import read_capsule_member_or_none, read_capsule_text_or_none

    cap = _write_capsule(tmp_path / RUN)
    (cap / "model-calls.jsonl").unlink()
    (cap / "model-calls.jsonl").symlink_to(secret)
    with caplog.at_level("WARNING", logger="novafabric._capsule_fs"):
        assert read_capsule_text_or_none(cap, "model-calls.jsonl") is None
        assert read_capsule_member_or_none(cap / "model-calls.jsonl") is None
    assert [r.message for r in caplog.records] == ["capsule_file_not_followed"] * 2
    assert read_capsule_text_or_none(cap, "absent.jsonl") is None  # missing: not logged
    assert len(caplog.records) == 2
