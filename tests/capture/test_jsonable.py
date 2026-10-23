"""``to_jsonable``: what the hooks write for values the caller handed the SDK."""

from __future__ import annotations

import enum
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from novafabric.capture._jsonable import MAX_DEPTH, json_default, placeholder, to_jsonable


class _Colour(enum.Enum):
    RED = "red"


class _DumpOnly:
    """Has ``model_dump`` but is not pydantic (e.g. an SDK's own base model)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"role": "assistant", "content": "hi"}


class _OldDump:
    """A ``model_dump`` that takes none of pydantic's keyword arguments."""

    def model_dump(self) -> dict[str, Any]:
        return {"ok": True}


class _Hostile:
    def model_dump(self, **kwargs: Any) -> Any:
        raise RuntimeError("boom")


def test_plain_json_round_trips_equal() -> None:
    value = {"a": [1, 2.5, "x", None, True], "b": {"c": []}}
    assert to_jsonable(value) == value


def test_model_dump_is_called_like_the_sdk_serialises_params() -> None:
    obj = _DumpOnly()
    assert to_jsonable(obj) == {"role": "assistant", "content": "hi"}
    assert obj.calls == [{"mode": "json", "exclude_unset": True}]


def test_model_dump_without_pydantic_kwargs_falls_back() -> None:
    assert to_jsonable(_OldDump()) == {"ok": True}


def test_pydantic_model_drops_unset_fields() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Msg(pydantic.BaseModel):
        role: str
        content: str | None = None
        refusal: str | None = None

    assert to_jsonable(Msg(role="assistant", content="hi")) == {
        "role": "assistant", "content": "hi",
    }


def test_hostile_object_becomes_a_placeholder_never_raises() -> None:
    out = to_jsonable({"x": _Hostile()})
    assert out == {"x": placeholder(_Hostile())}
    assert "_Hostile" in out["x"]


def test_scalars_and_containers() -> None:
    u = uuid.UUID(int=1)
    out = to_jsonable({
        "e": _Colour.RED, "d": Decimal("1.50"), "u": u, "t": (1, 2),
        "s": frozenset({3}), "b": bytearray(b"abc"),
    })
    assert out == {"e": "red", "d": "1.50", "u": str(u), "t": [1, 2], "s": [3],
                   "b": "<bytes: 3>"}


def test_non_string_keys_are_made_serialisable() -> None:
    out = to_jsonable({(1, 2): "tuple-key", _Colour.RED: "enum-key", 7: "int-key"})
    json.dumps(out)
    assert out[_Colour.RED.value] == "enum-key"
    assert out[7] == "int-key"


def test_reference_cycles_and_depth_are_bounded() -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    json.dumps(to_jsonable(cyclic))  # terminates and serialises

    deep: Any = "leaf"
    for _ in range(MAX_DEPTH + 10):
        deep = [deep]
    flat = json.dumps(to_jsonable(deep))
    assert "leaf" not in flat and "unserializable" in flat


def test_json_default_is_the_dumps_hook() -> None:
    assert json.dumps({"m": _DumpOnly()}, default=json_default) == (
        '{"m": {"role": "assistant", "content": "hi"}}'
    )
