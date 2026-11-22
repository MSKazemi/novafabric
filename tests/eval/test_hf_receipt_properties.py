"""Property tests for the untrusted-input edges of the Hugging Face eval receipts (HF-05).

The example tests pin named cases. These generate arbitrary text and structures and assert the
contract: the card backlink never alters a byte outside its own marked block and is idempotent, a
damaged card is refused rather than guessed at, and the ``.eval_results`` validator reports problems
as data -- it never raises on whatever a YAML file decodes to.
"""
from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from novafabric.eval.hf_receipt import (
    CARD_BLOCK_BEGIN,
    CARD_BLOCK_END,
    EvalReceiptError,
    append_card_backlink,
    validate_eval_results,
)

_free = (
    st.lists(
        st.sampled_from([*"abc \n\r\t#-*`|<>!:", "---\n", "license: mit\n", "## Eval\n"]), max_size=14
    )
    .map("".join)
    .filter(lambda s: CARD_BLOCK_BEGIN not in s and CARD_BLOCK_END not in s)
)
_body = _free.map(lambda b: b.replace("\r", ""))


def _section(body: str) -> str:
    return f"{CARD_BLOCK_BEGIN}\n{body}\n{CARD_BLOCK_END}\n"


@settings(max_examples=400, deadline=None)
@given(card=_free, body=_body)
def test_appending_keeps_every_original_byte_and_is_idempotent(card: str, body: str) -> None:
    section = _section(body)
    once = append_card_backlink(card, section)
    assert once.startswith(card) and once.endswith(section)
    assert append_card_backlink(once, section) == once


@settings(max_examples=400, deadline=None)
@given(before=_free, after=_free, old=_body, new=_body)
def test_replacing_a_block_touches_nothing_outside_it(
    before: str, after: str, old: str, new: str
) -> None:
    card = before + _section(old) + after
    assert append_card_backlink(card, _section(new)) == before + _section(new) + after


_markers = st.sampled_from([CARD_BLOCK_BEGIN, CARD_BLOCK_END])


@settings(max_examples=400, deadline=None)
@given(pieces=st.lists(st.one_of(_free, _markers), max_size=7), body=_body)
def test_a_card_with_markers_in_any_order_is_replaced_or_refused_never_a_crash(
    pieces: list[str], body: str
) -> None:
    card = "".join(pieces)
    try:
        result = append_card_backlink(card, _section(body))
    except EvalReceiptError:
        return  # damaged / ambiguous: refused, as documented
    assert result.count(CARD_BLOCK_BEGIN) >= 1 and _section(body) in result


_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=True) | st.text(max_size=20),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=8), inner, max_size=4),
    max_leaves=25,
)


@settings(max_examples=600, deadline=None)
@given(entries=_json)
def test_the_validator_reports_problems_as_data_and_never_raises(entries: object) -> None:
    problems = validate_eval_results(entries)
    assert isinstance(problems, list) and all(isinstance(p, str) for p in problems)


@settings(max_examples=300, deadline=None)
@given(extra=st.dictionaries(st.text(max_size=8), _json, max_size=3))
def test_a_verify_token_is_never_accepted_whatever_else_is_in_the_entry(
    extra: dict[str, object],
) -> None:
    entry = {
        "dataset": {"id": "acme/qa", "task_id": "t"},
        "value": 1.0,
        "verifyToken": "x",
        **{k: v for k, v in extra.items() if k not in {"dataset", "value", "verifyToken"}},
    }
    assert validate_eval_results([entry]) != []
