"""JSON-safe conversion for values the capture layer did not create.

The SDK hooks record the caller's own ``kwargs``. Those routinely carry SDK
objects rather than plain dicts: the everyday multi-turn pattern appends
``completion.choices[0].message`` (an ``openai`` pydantic model) to the next
call's ``messages``, and the SDKs accept that. ``json.dumps`` does not, so a
capsule write used to raise ``TypeError`` into the user's workload *after* the
provider had answered.

:func:`to_jsonable` turns such a value into what the SDK itself would have sent
on the wire. Pydantic v2 models are dumped the way the ``openai`` and
``anthropic`` SDKs serialise request params (``model_dump(mode="json",
exclude_unset=True)``), so the SDK record's messages equal the wire record's.
Anything with no faithful JSON form becomes a placeholder naming its type --
never its ``repr``, which may carry anything (a key, a whole document).

Pure, no I/O, never raises, and bounded: recursion stops at
:data:`MAX_DEPTH`, which also terminates reference cycles.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import uuid
from collections.abc import Mapping
from decimal import Decimal
from pathlib import PurePath
from typing import Any

#: Deeper values are replaced by a placeholder (also ends reference cycles).
MAX_DEPTH = 64

_PRIMITIVES = (str, int, float, bool, type(None))


def placeholder(value: Any) -> str:
    """The stand-in written for a value with no faithful JSON form."""
    kind = type(value)
    return f"<unserializable: {kind.__module__}.{kind.__qualname__}>"


def _key(key: Any) -> str | int | float | bool | None:
    """A dict key ``json.dumps`` accepts (it never calls ``default`` on keys)."""
    if isinstance(key, _PRIMITIVES):
        return key
    if isinstance(key, enum.Enum) and isinstance(key.value, _PRIMITIVES):
        return key.value
    return str(key) if isinstance(key, (PurePath, uuid.UUID)) else placeholder(key)


def _model_dump(value: Any) -> tuple[bool, Any]:
    """``(True, dumped)`` for a pydantic v2/v1-shaped model, else ``(False, None)``."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return True, dump(mode="json", exclude_unset=True)
        except TypeError:  # a model_dump that does not take pydantic's kwargs
            return True, dump()
    legacy = getattr(value, "dict", None)
    if callable(legacy) and hasattr(value, "__fields__"):  # pydantic v1
        return True, legacy(exclude_unset=True)
    return False, None


def to_jsonable(value: Any, _depth: int = 0) -> Any:
    """Return a JSON-serialisable copy of *value*. Never raises.

    Plain JSON values come back as equal values (containers are copied);
    ``tuple``/``set`` become lists; ``datetime``/``date``/``time`` ISO strings;
    ``Path``/``UUID``/``Decimal`` strings; an ``Enum`` its value; ``bytes`` a
    length placeholder; pydantic models and dataclasses their field dict.
    """
    try:
        if isinstance(value, _PRIMITIVES):
            return value
        if _depth >= MAX_DEPTH:
            return placeholder(value)
        nxt = _depth + 1
        if isinstance(value, Mapping):
            return {_key(k): to_jsonable(v, nxt) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [to_jsonable(v, nxt) for v in value]
        if isinstance(value, (set, frozenset)):
            return [to_jsonable(v, nxt) for v in value]
        is_model, dumped = _model_dump(value)
        if is_model:
            return to_jsonable(dumped, nxt)
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            fields = {f.name: getattr(value, f.name) for f in dataclasses.fields(value)}
            return to_jsonable(fields, nxt)
        if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
            return value.isoformat()
        if isinstance(value, enum.Enum):
            return to_jsonable(value.value, nxt)
        if isinstance(value, (PurePath, uuid.UUID, Decimal)):
            return str(value)
        if isinstance(value, (bytes, bytearray, memoryview)):
            return f"<bytes: {len(value)}>"
        return placeholder(value)
    except Exception:  # noqa: BLE001 -- a hostile __getattr__/model_dump must not escape
        return placeholder(value)


def json_default(value: Any) -> Any:
    """``json.dumps(default=...)`` hook: called only for non-JSON values."""
    return to_jsonable(value)
