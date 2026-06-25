from __future__ import annotations

from novafabric.diff._align import align_model_calls, align_tool_calls


def _mc(span_id: str, call_id: str = "") -> dict:
    return {"model_call_id": call_id or span_id, "parent_span_id": span_id}


def _tc(tool_name: str, call_id: str, arguments: dict | None = None) -> dict:
    return {"tool_call_id": call_id, "tool_name": tool_name, "arguments": arguments or {}}


class TestAlignModelCalls:
    def test_identical_lists(self) -> None:
        calls = [_mc("span1"), _mc("span2")]
        pairs = align_model_calls(calls, calls)
        assert len(pairs) == 2
        for a, b in pairs:
            assert a is not None and b is not None

    def test_empty_both(self) -> None:
        assert align_model_calls([], []) == []

    def test_added_call_in_b(self) -> None:
        a = [_mc("span1")]
        b = [_mc("span1"), _mc("span2")]
        pairs = align_model_calls(a, b)
        # span1 matched, span2 unmatched (added)
        matched = [(x, y) for x, y in pairs if x is not None and y is not None]
        added = [(x, y) for x, y in pairs if x is None]
        assert len(matched) == 1
        assert len(added) == 1

    def test_removed_call_in_b(self) -> None:
        a = [_mc("span1"), _mc("span2")]
        b = [_mc("span1")]
        pairs = align_model_calls(a, b)
        removed = [(x, y) for x, y in pairs if y is None]
        assert len(removed) == 1
        assert removed[0][0]["parent_span_id"] == "span2"

    def test_different_spans_no_match(self) -> None:
        a = [_mc("spanA")]
        b = [_mc("spanB")]
        pairs = align_model_calls(a, b)
        # spanA removed, spanB added
        assert any(x is not None and y is None for x, y in pairs)
        assert any(x is None and y is not None for x, y in pairs)


class TestAlignToolCalls:
    def test_matched_by_name_and_args(self) -> None:
        a = [_tc("search", "ID1", {"q": "hello"})]
        b = [_tc("search", "ID2", {"q": "hello"})]  # different ID, same args
        pairs = align_tool_calls(a, b)
        assert len(pairs) == 1
        x, y = pairs[0]
        assert x is not None and y is not None
        assert x["tool_call_id"] == "ID1"
        assert y["tool_call_id"] == "ID2"

    def test_added_tool(self) -> None:
        a = [_tc("fetch", "A", {"url": "http://x"})]
        b = [_tc("fetch", "A", {"url": "http://x"}), _tc("post", "B", {})]
        pairs = align_tool_calls(a, b)
        added = [p for p in pairs if p[0] is None]
        assert len(added) == 1
        assert added[0][1]["tool_name"] == "post"

    def test_empty_both(self) -> None:
        assert align_tool_calls([], []) == []

    def test_arg_hash_order_independent(self) -> None:
        a = [_tc("call", "A", {"b": 2, "a": 1})]
        b = [_tc("call", "B", {"a": 1, "b": 2})]
        pairs = align_tool_calls(a, b)
        assert len(pairs) == 1
        x, y = pairs[0]
        assert x is not None and y is not None
