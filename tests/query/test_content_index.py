"""Tests for the capsule content FTS5 index (ADR-0204 P1, experimental).

Covers: extraction (happy path, capsule without content, size bounds),
the corpus-subset guard, MATCH escaping (operator injection attempts match
literally, never error), idempotent reindex + GC, the redaction invariant
(raw secret never reaches registry.db), ingest wiring, and clean
degradation when the sqlite build lacks FTS5.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from novafabric.query import content_index as ci
from novafabric.query.content_index import (
    ContentIndexUnavailableError,
    Doc,
    EmptyQueryError,
    StreamNotAllowedError,
    build_match_expression,
    delete_run,
    ensure_content_index,
    extract_docs,
    fts5_available,
    index_capsule,
    index_docs,
    maybe_index_capsule,
    reindex,
    search_content,
)

requires_fts5 = pytest.mark.skipif(
    not fts5_available(), reason="sqlite build lacks FTS5"
)


# ── fixture capsule builder ───────────────────────────────────────────────


def make_capsule(
    base: Path,
    run_id: str,
    *,
    created_at: str = "2026-07-24T10:00:00Z",
    status: str = "success",
    message: str = "find invoice INV-2291 and pay it",
    reply: str = "Found invoice INV-2291 for café supplies",
) -> Path:
    cap = base / run_id
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text(yaml.dump({
        "run_id": run_id,
        "status": status,
        "created_at": created_at,
        "command": ["python", "agent.py"],
        "tags": {"team": "billing"},
        "metadata": {"note": "quarterly sweep"},
    }))
    (cap / "model-calls.jsonl").write_text(json.dumps({
        "gen_ai.request.messages": [{"role": "user", "content": message}],
        "gen_ai.response.choices": [{"message": {"content": reply}}],
    }) + "\n")
    (cap / "tool-calls.jsonl").write_text(json.dumps({
        "tool_name": "shell",
        "arguments": {"cmd": "rm -rf /tmp/scratch"},
        "result": "removed 3 files",
    }) + "\n")
    (cap / "trace.jsonl").write_text(json.dumps({
        "name": "agent.step",
        "attributes": {"note": "retried after 429 rate limit"},
    }) + "\n")
    return cap


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    from novafabric.registry.runs_cache import ensure_runs_cache

    c = sqlite3.connect(str(tmp_path / "registry.db"))
    ensure_runs_cache(c)
    yield c
    c.close()


# ── extraction ────────────────────────────────────────────────────────────


class TestExtraction:
    def test_happy_path_extracts_all_four_streams(self, tmp_path: Path) -> None:
        cap = make_capsule(tmp_path, "r1")
        docs, skipped = extract_docs(cap, "r1")
        assert skipped == 0
        streams = {d.stream for d in docs}
        assert streams == {
            "model-call-messages", "tool-call-arguments", "trace", "capsule-yaml",
        }
        model_doc = next(d for d in docs if d.stream == "model-call-messages")
        assert "INV-2291" in model_doc.text
        assert "café" in model_doc.text
        assert model_doc.line_no == 1
        tool_doc = next(d for d in docs if d.stream == "tool-call-arguments")
        assert "shell" in tool_doc.text
        assert "rm -rf" in tool_doc.text
        assert "removed 3 files" in tool_doc.text
        yaml_doc = next(d for d in docs if d.stream == "capsule-yaml")
        assert "python agent.py" in yaml_doc.text
        assert "billing" in yaml_doc.text
        assert yaml_doc.line_no is None

    def test_capsule_without_content_yields_manifest_doc_only(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        """A minimal-level capture (no JSONL streams) is not an error."""
        cap = tmp_path / "r-min"
        cap.mkdir()
        (cap / "capsule.yaml").write_text(yaml.dump({
            "run_id": "r-min", "status": "success", "command": ["echo", "hi"],
        }))
        docs, skipped = extract_docs(cap, "r-min")
        assert skipped == 0
        assert [d.stream for d in docs] == ["capsule-yaml"]
        if fts5_available():
            result = index_capsule(conn, cap, "r-min")
            assert result.doc_count == 1

    def test_capsule_with_no_extractable_text_yields_zero_docs(
        self, tmp_path: Path
    ) -> None:
        cap = tmp_path / "r-empty"
        cap.mkdir()
        (cap / "capsule.yaml").write_text(yaml.dump({"run_id": "r-empty"}))
        (cap / "model-calls.jsonl").write_text(
            json.dumps({"gen_ai.usage.input_tokens": 5}) + "\n"
        )
        docs, skipped = extract_docs(cap, "r-empty")
        assert docs == []
        assert skipped == 0

    def test_oversized_field_is_truncated_at_utf8_boundary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CONTENT_INDEX_MAX_DOC_KB", "1")
        # 2-byte UTF-8 char repeated: an odd byte cap would split a char.
        big = "é" * 4000
        cap = make_capsule(tmp_path, "r-big", message=big)
        docs, skipped = extract_docs(cap, "r-big")
        assert skipped == 0  # truncation is not a skip
        model_doc = next(d for d in docs if d.stream == "model-call-messages")
        raw = model_doc.text.encode("utf-8")
        assert len(raw) <= 1024
        model_doc.text.encode("utf-8")  # still valid UTF-8 round-trip

    def test_max_docs_per_capsule_drops_and_counts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CONTENT_INDEX_MAX_DOCS", "2")
        cap = make_capsule(tmp_path, "r-cap")
        docs, skipped = extract_docs(cap, "r-cap")
        assert len(docs) == 2
        assert skipped >= 2  # the remaining streams were dropped, counted

    def test_malformed_jsonl_line_is_skipped_not_fatal(
        self, tmp_path: Path
    ) -> None:
        cap = make_capsule(tmp_path, "r-bad")
        (cap / "trace.jsonl").write_text(
            "NOT JSON{{{\n"
            + json.dumps({"name": "good.span"}) + "\n"
            + json.dumps(["a", "list"]) + "\n"
        )
        docs, skipped = extract_docs(cap, "r-bad")
        trace_docs = [d for d in docs if d.stream == "trace"]
        assert [d.text for d in trace_docs] == ["good.span"]
        assert [d.line_no for d in trace_docs] == [2]
        assert skipped == 2  # malformed line + non-dict record

    def test_absent_capsule_yaml_stream_tolerated(self, tmp_path: Path) -> None:
        cap = tmp_path / "r-noyaml"
        cap.mkdir()
        (cap / "trace.jsonl").write_text(json.dumps({"name": "s"}) + "\n")
        docs, skipped = extract_docs(cap, "r-noyaml")
        assert [d.stream for d in docs] == ["trace"]
        assert skipped == 0


# ── corpus-subset guard ───────────────────────────────────────────────────


def test_corpus_is_pinned_subset_of_scan_targets() -> None:
    """ADR-0209 coordination: the scanner now covers the extended event
    streams, but the v0 search corpus stays pinned to the original four.
    Invariant 1 (corpus ⊆ SCAN_TARGETS) must keep holding as either side
    evolves; expanding the corpus is ADR-0204 P2 + a corpus_rev bump."""
    from novafabric.capture.secrets import SCAN_TARGETS

    assert ci._CORPUS == (
        ("model-calls.jsonl", "model-call-messages"),
        ("tool-calls.jsonl", "tool-call-arguments"),
        ("trace.jsonl", "trace"),
        ("capsule.yaml", "capsule-yaml"),
    )
    assert set(ci._CORPUS) < set(SCAN_TARGETS)  # strict subset since ADR-0209


def test_extended_event_streams_are_not_extracted(tmp_path: Path) -> None:
    """A capsule carrying ADR-0209 extended streams yields no docs for them —
    they are scanner-covered but outside the pinned v0 corpus."""
    cap = make_capsule(tmp_path, "r-ext")
    (cap / "guardrail_events.jsonl").write_text(json.dumps({
        "event_type": "GuardrailEvaluated", "guardrail_name": "pii",
        "outcome": "blocked", "details": {"note": "should not be indexed"},
    }) + "\n")
    (cap / "vector_retrievals.jsonl").write_text(json.dumps({
        "event_type": "VectorRetrievalCompleted", "vector_store": "qdrant",
    }) + "\n")
    docs, skipped = extract_docs(cap, "r-ext")
    assert {d.stream for d in docs} == {
        "model-call-messages", "tool-call-arguments", "trace", "capsule-yaml",
    }
    assert skipped == 0


@requires_fts5
def test_indexer_refuses_stream_outside_scan_targets(
    conn: sqlite3.Connection,
) -> None:
    bad = Doc(run_id="r", stream="guardrail-events", ref="guardrail_events.jsonl",
              line_no=1, text="unscanned text")
    with pytest.raises(StreamNotAllowedError):
        index_docs(conn, "r", [bad])
    # Refusal happened before any write.
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='capsule_docs'"
    ).fetchone()
    if row is not None:
        assert conn.execute("SELECT COUNT(*) FROM capsule_docs").fetchone()[0] == 0


@requires_fts5
def test_search_rejects_unknown_stream_filter(
    conn: sqlite3.Connection,
) -> None:
    ensure_content_index(conn)
    with pytest.raises(StreamNotAllowedError):
        search_content(conn, "x", stream="guardrail-events")


# ── MATCH escaping ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("user_input", "expected"),
    [
        ("invoice 2291", '"invoice" "2291"'),
        ("rm -rf", '"rm" "-rf"'),
        ("inv*", '"inv"*'),
        ('say "hello"', '"say" """hello"""'),
        ("a OR b", '"a" "OR" "b"'),
        ("NEAR(x)", '"NEAR(x)"'),
        ("text: value", '"text:" "value"'),
        ("*", '"*"'),
    ],
)
def test_match_expression_table(user_input: str, expected: str) -> None:
    assert build_match_expression(user_input) == expected


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_empty_query_rejected(bad: str) -> None:
    with pytest.raises(EmptyQueryError):
        build_match_expression(bad)


@requires_fts5
class TestMatchEscapingAgainstIndex:
    @pytest.fixture()
    def searchable(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> sqlite3.Connection:
        make_capsule(tmp_path, "r1")
        cap2 = tmp_path / "r2"
        cap2.mkdir()
        (cap2 / "capsule.yaml").write_text(yaml.dump({
            "run_id": "r2", "command": ["echo"],
            "metadata": {"note": "alpha OR beta NEAR gamma"},
        }))
        index_capsule(conn, tmp_path / "r1", "r1")
        index_capsule(conn, cap2, "r2")
        return conn

    def test_operators_match_literally_not_as_syntax(
        self, searchable: sqlite3.Connection
    ) -> None:
        # 'rm -rf' must NOT exclude 'rf' — it matches the literal text.
        hits = search_content(searchable, "rm -rf")
        assert [h.run_id for h in hits] == ["r1"]
        # Literal OR: all three terms must match — only r2 has the token 'OR'.
        hits = search_content(searchable, "alpha OR beta")
        assert [h.run_id for h in hits] == ["r2"]
        # AND semantics: 'alpha invoice' spans two runs → no hit.
        assert search_content(searchable, "alpha invoice") == []

    @pytest.mark.parametrize(
        "injection",
        [
            'x" OR "y', '"', "*", "-", "(", ":", "OR", "NEAR", "NOT x",
            "a AND b", "text:secret", "^start", '""" """', "col:*",
            "NEAR(a b, 5)", "- - -",
        ],
    )
    def test_injection_attempts_never_raise_sqlite_errors(
        self, searchable: sqlite3.Connection, injection: str
    ) -> None:
        hits = search_content(searchable, injection)  # must not raise
        assert isinstance(hits, list)

    def test_prefix_search(self, searchable: sqlite3.Connection) -> None:
        hits = search_content(searchable, "inv*")
        assert [h.run_id for h in hits] == ["r1"]

    def test_diacritic_insensitive(self, searchable: sqlite3.Connection) -> None:
        hits = search_content(searchable, "cafe")
        assert [h.run_id for h in hits] == ["r1"]

    def test_snippet_carries_markers(self, searchable: sqlite3.Connection) -> None:
        hits = search_content(searchable, "invoice")
        snippets = [m.snippet for h in hits for m in h.matches]
        assert any("«invoice»" in s.lower() for s in snippets)


# ── indexing, reindex, deletion ───────────────────────────────────────────


@requires_fts5
class TestIndexLifecycle:
    def test_index_capsule_writes_state_row_last(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        cap = make_capsule(tmp_path, "r1")
        result = index_capsule(conn, cap, "r1")
        assert result.doc_count == 4
        assert result.skipped == 0
        state = conn.execute(
            "SELECT doc_count, skipped, corpus_rev FROM capsule_index_state"
            " WHERE run_id='r1'"
        ).fetchone()
        assert state == (4, 0, "v0")

    def test_reindex_is_idempotent(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        make_capsule(tmp_path, "r1")
        make_capsule(tmp_path, "r2", message="second capsule payload")
        stats1 = reindex(conn, tmp_path)
        assert stats1.indexed == 2
        count1 = conn.execute("SELECT COUNT(*) FROM capsule_docs").fetchone()[0]
        hits1 = [h.run_id for h in search_content(conn, "invoice")]

        stats2 = reindex(conn, tmp_path)
        assert stats2.indexed == 0  # nothing new; state rows current
        count2 = conn.execute("SELECT COUNT(*) FROM capsule_docs").fetchone()[0]
        hits2 = [h.run_id for h in search_content(conn, "invoice")]
        assert count2 == count1
        assert hits2 == hits1

        stats3 = reindex(conn, tmp_path, force=True)
        assert stats3.indexed == 2  # forced re-extraction, still converges
        assert conn.execute(
            "SELECT COUNT(*) FROM capsule_docs"
        ).fetchone()[0] == count1

    def test_reindex_garbage_collects_deleted_capsules(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        import shutil

        cap = make_capsule(tmp_path, "r-gone")
        make_capsule(
            tmp_path, "r-stays",
            message="staying around", reply="nothing else here",
        )
        reindex(conn, tmp_path)
        assert [h.run_id for h in search_content(conn, "invoice")] == ["r-gone"]

        shutil.rmtree(cap)
        stats = reindex(conn, tmp_path)
        assert stats.removed == 1
        assert search_content(conn, "invoice") == []
        assert conn.execute(
            "SELECT COUNT(*) FROM capsule_docs WHERE run_id='r-gone'"
        ).fetchone()[0] == 0
        assert search_content(conn, "staying")

    def test_delete_run_is_idempotent_and_safe_without_tables(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        delete_run(conn, "never-indexed")  # tables absent — no-op
        cap = make_capsule(tmp_path, "r1")
        index_capsule(conn, cap, "r1")
        delete_run(conn, "r1")
        delete_run(conn, "r1")
        assert search_content(conn, "invoice") == []
        assert conn.execute(
            "SELECT COUNT(*) FROM capsule_index_state"
        ).fetchone()[0] == 0

    def test_search_filters_join_runs_cache(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        from novafabric.registry.runs_cache import upsert_run

        make_capsule(tmp_path, "r-old", created_at="2026-01-01T00:00:00Z")
        make_capsule(
            tmp_path, "r-new",
            created_at="2026-07-01T00:00:00Z", status="failure",
        )
        for rid, ts, st in [
            ("r-old", "2026-01-01T00:00:00Z", "success"),
            ("r-new", "2026-07-01T00:00:00Z", "failure"),
        ]:
            upsert_run(conn, {
                "run_id": rid, "created_at": ts, "status": st, "command": [],
            })
        reindex(conn, tmp_path)

        assert {h.run_id for h in search_content(conn, "invoice")} == {
            "r-old", "r-new",
        }
        assert [h.run_id for h in search_content(
            conn, "invoice", since="2026-06-01T00:00:00Z"
        )] == ["r-new"]
        assert [h.run_id for h in search_content(
            conn, "invoice", until="2026-02-01T00:00:00Z"
        )] == ["r-old"]
        assert [h.run_id for h in search_content(
            conn, "invoice", status="failure"
        )] == ["r-new"]
        assert [h.run_id for h in search_content(
            conn, "shell", stream="tool-call-arguments"
        )] != []
        assert search_content(conn, "shell", stream="capsule-yaml") == []

    def test_matches_per_run_capped_and_flagged(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        cap = tmp_path / "r-many"
        cap.mkdir()
        (cap / "capsule.yaml").write_text(yaml.dump({"run_id": "r-many"}))
        lines = [
            json.dumps({"name": f"needle span {i}"}) for i in range(12)
        ]
        (cap / "trace.jsonl").write_text("\n".join(lines) + "\n")
        index_capsule(conn, cap, "r-many")
        hits = search_content(conn, "needle")
        assert len(hits) == 1
        assert len(hits[0].matches) == 8
        assert hits[0].matches_truncated is True

    def test_limit_caps_number_of_runs(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        for i in range(5):
            make_capsule(tmp_path, f"r{i}")
        reindex(conn, tmp_path)
        assert len(search_content(conn, "invoice", limit=3)) == 3


# ── redaction invariant ───────────────────────────────────────────────────


@requires_fts5
def test_raw_secret_never_reaches_registry_db(tmp_path: Path) -> None:
    """ADR-0204 acceptance: index only post-redaction bytes.

    A capsule with a seeded Anthropic-shaped key is scanned+redacted the way
    every capture path does it, then ingested. The raw secret must appear
    nowhere in the DB file; the redaction marker must be findable.
    """
    from novafabric.capture.secrets import SecretScannerV0
    from novafabric.registry.runs_cache import ensure_runs_cache

    secret = "sk-ant-" + "a1b2c3d4e5" * 3
    cap = make_capsule(
        tmp_path, "r-sec", message=f"use api key {secret} to call the model"
    )
    SecretScannerV0(cap, "r-sec").scan_and_redact()

    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    try:
        ensure_runs_cache(conn)
        index_capsule(conn, cap, "r-sec")
        conn.commit()
        hits = search_content(conn, "REDACTED")
        assert [h.run_id for h in hits] == ["r-sec"]
    finally:
        conn.close()
    assert secret.encode() not in db.read_bytes()


# ── ingest wiring + opt-out ───────────────────────────────────────────────


@requires_fts5
class TestIngestWiring:
    def test_build_runs_index_populates_content_index(
        self, tmp_path: Path
    ) -> None:
        from novafabric.registry.runs_cache import (
            build_runs_index,
            ensure_runs_cache,
        )

        base = tmp_path / "capsules"
        base.mkdir()
        make_capsule(base, "r1")
        conn = sqlite3.connect(str(tmp_path / "registry.db"))
        try:
            ensure_runs_cache(conn)
            inserted = build_runs_index(base, conn)
            assert inserted == 1
            assert [h.run_id for h in search_content(conn, "invoice")] == ["r1"]
        finally:
            conn.close()

    def test_opt_out_env_disables_indexing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.registry.runs_cache import (
            build_runs_index,
            ensure_runs_cache,
        )

        monkeypatch.setenv("NOVA_CONTENT_INDEX", "off")
        base = tmp_path / "capsules"
        base.mkdir()
        make_capsule(base, "r1")
        conn = sqlite3.connect(str(tmp_path / "registry.db"))
        try:
            ensure_runs_cache(conn)
            assert build_runs_index(base, conn) == 1  # metadata unaffected
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='capsule_docs'"
            ).fetchone() is None
        finally:
            conn.close()

    def test_maybe_index_skips_already_indexed_run(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        cap = make_capsule(tmp_path, "r1")
        maybe_index_capsule(conn, cap, "r1")
        assert search_content(conn, "invoice")
        # Mutate the capsule; a repeat ingest hook must NOT re-extract
        # (capsules are immutable post-capture; --reindex --all forces it).
        (cap / "trace.jsonl").write_text(
            json.dumps({"name": "mutated-after-capture"}) + "\n"
        )
        maybe_index_capsule(conn, cap, "r1")
        assert search_content(conn, "mutated-after-capture") == []

    def test_maybe_index_is_fail_open(
        self, tmp_path: Path, conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def boom(*a: object, **k: object) -> None:
            raise RuntimeError("indexer exploded")

        monkeypatch.setattr(ci, "index_capsule", boom)
        cap = make_capsule(tmp_path, "r1")
        maybe_index_capsule(conn, cap, "r1")  # must not raise


# ── FTS5-unavailable degradation ──────────────────────────────────────────


class TestFts5Unavailable:
    @pytest.fixture()
    def no_fts5(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci, "fts5_available", lambda: False)

    def test_ensure_raises_named_error(
        self, no_fts5: None, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(ContentIndexUnavailableError):
            ensure_content_index(conn)

    def test_search_raises_named_error(
        self, no_fts5: None, conn: sqlite3.Connection
    ) -> None:
        with pytest.raises(ContentIndexUnavailableError):
            search_content(conn, "anything")

    def test_ingest_hook_degrades_silently(
        self, no_fts5: None, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        cap = make_capsule(tmp_path, "r1")
        maybe_index_capsule(conn, cap, "r1")  # no-op, no error
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='capsule_docs'"
        ).fetchone() is None

    def test_probe_caches_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(ci, "_fts5_probe_result", None)
        first = fts5_available()
        assert fts5_available() is first


# ── coverage of tolerant branches and env knobs ───────────────────────────


class TestExtractionVariants:
    def test_env_knob_garbage_falls_back_to_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_CONTENT_INDEX_MAX_DOC_KB", "not-a-number")
        monkeypatch.setenv("NOVA_CONTENT_INDEX_MAX_DOCS", "also-not")
        assert ci._max_doc_bytes() == ci.DEFAULT_MAX_DOC_BYTES
        assert ci._max_docs_per_capsule() == ci.DEFAULT_MAX_DOCS_PER_CAPSULE

    def test_message_parts_lists_and_choice_content(self, tmp_path: Path) -> None:
        cap = tmp_path / "r-parts"
        cap.mkdir()
        (cap / "capsule.yaml").write_text(yaml.dump({
            "run_id": "r-parts",
            "command": "single-string-command",
            "tags": ["taglist-entry"],
        }))
        (cap / "model-calls.jsonl").write_text(json.dumps({
            "gen_ai.request.messages": [
                {"role": "user", "content": [
                    "plain part", {"text": "typed part"}, {"media": {}}, 42,
                ]},
                "not-a-dict-message",
            ],
            "gen_ai.response.choices": [
                {"content": "choice-level content"},
                "not-a-dict-choice",
            ],
        }) + "\n")
        (cap / "tool-calls.jsonl").write_text(json.dumps({
            "name": "fallback-name",
            "arguments": "raw-string-args",
            "output": "string-output",
            "summary": "string-summary",
            "result": ["structured", "result"],
        }) + "\n")
        docs, skipped = extract_docs(cap, "r-parts")
        assert skipped == 0
        model = next(d for d in docs if d.stream == "model-call-messages")
        assert "plain part" in model.text
        assert "typed part" in model.text
        assert "choice-level content" in model.text
        tool = next(d for d in docs if d.stream == "tool-call-arguments")
        for expected in (
            "fallback-name", "raw-string-args", "string-output",
            "string-summary", "structured",
        ):
            assert expected in tool.text
        cy = next(d for d in docs if d.stream == "capsule-yaml")
        assert "single-string-command" in cy.text
        assert "taglist-entry" in cy.text

    def test_invalid_and_non_dict_capsule_yaml_counted_skipped(
        self, tmp_path: Path
    ) -> None:
        cap1 = tmp_path / "r-yamlbad"
        cap1.mkdir()
        (cap1 / "capsule.yaml").write_text("{unbalanced: [")
        docs, skipped = extract_docs(cap1, "r-yamlbad")
        assert docs == [] and skipped == 1

        cap2 = tmp_path / "r-yamllist"
        cap2.mkdir()
        (cap2 / "capsule.yaml").write_text(yaml.dump(["a", "list"]))
        docs, skipped = extract_docs(cap2, "r-yamllist")
        assert docs == [] and skipped == 1

    def test_oversized_source_file_skipped_entirely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ci, "MAX_SOURCE_FILE_BYTES", 10)
        cap = make_capsule(tmp_path, "r-huge")
        docs, skipped = extract_docs(cap, "r-huge")
        assert docs == []
        assert skipped == 4  # every stream file exceeds the 10-byte cap


@requires_fts5
class TestReindexTolerance:
    def test_reindex_skips_unloadable_manifest_dirs(
        self, tmp_path: Path, conn: sqlite3.Connection
    ) -> None:
        make_capsule(tmp_path, "r-good")
        bad = tmp_path / "r-badyaml"
        bad.mkdir()
        (bad / "capsule.yaml").write_text("{unbalanced: [")
        stats = reindex(conn, tmp_path)
        assert stats.indexed == 1
        assert [h.run_id for h in search_content(conn, "invoice")] == ["r-good"]

    def test_reindex_records_per_capsule_errors(
        self, tmp_path: Path, conn: sqlite3.Connection,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        make_capsule(tmp_path, "r-err")

        def boom(*a: object, **k: object) -> None:
            raise ci.ContentSearchError("synthetic index failure")

        monkeypatch.setattr(ci, "index_capsule", boom)
        stats = reindex(conn, tmp_path)
        assert stats.indexed == 0
        assert stats.errors == ["r-err: synthetic index failure"]


@requires_fts5
def test_unexpected_match_error_becomes_named_error(
    tmp_path: Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth: an FTS5 syntax error never escapes as sqlite3."""
    cap = make_capsule(tmp_path, "r1")
    index_capsule(conn, cap, "r1")
    monkeypatch.setattr(ci, "build_match_expression", lambda q: "AND AND")
    with pytest.raises(ci.ContentSearchError):
        search_content(conn, "anything")
