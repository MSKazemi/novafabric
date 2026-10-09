"""Canonical arguments and the JSON result codec for ``record.tool`` (ADR-0306 D3, D6).

Applied identically at capture (``hooks/_python_tool``) and in a mocked replay
(``replay/_dispatcher``), so a replayed call and its record hash the same way.
Pure functions, no import-time IO, and nothing here ever imports, unpickles or
evaluates a name taken from a capsule: a value is JSON-native or it is refused.
"""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import math
import os
from dataclasses import dataclass
from typing import Any

from novafabric.capture._tool_scope import WITHIN_TOOL_CALL_EXT as WITHIN_TOOL_CALL_EXT

#: Surface marker written into a python-surface record's ``extensions``.
TOOL_SURFACE_EXT = "io.novafabric.tool_surface"
TOOL_SURFACE_PYTHON_FUNCTION = "python.function"
#: Surface marker of a Google ADK tool call recorded through the NovaFabric ADK
#: tool plugin (ADR-0306 slice 3, ``adapters/_adk_tool_seam``).
TOOL_SURFACE_ADK_TOOL = "google.adk.tool"
QUALNAME_EXT = "io.novafabric.tool_qualname"
RESULT_CODEC_EXT = "io.novafabric.result_codec"
NOT_SERVABLE_REASON_EXT = "io.novafabric.not_servable_reason"
RESULT_DIGEST_EXT = "io.novafabric.result_digest"
IGNORED_ARGUMENTS_EXT = "io.novafabric.ignored_arguments"
EXCEPTION_BUILTIN_EXT = "io.novafabric.exception_builtin"
NESTED_RECORDS_EXT = "io.novafabric.nested_records"
#: Written only when payload capture was off: the digest replay matches on,
#: because the arguments themselves were not recorded.
ARGUMENTS_DIGEST_EXT = "io.novafabric.arguments_digest"

CODEC_JSON = "json-v1"
CODEC_NOT_SERVABLE = "not-servable"

#: Env var for the result size cap (ADR-0306 D9; default 1 MiB, open question 6).
RESULT_MAX_BYTES_ENV = "NOVAFABRIC_TOOL_RESULT_MAX_BYTES"
DEFAULT_RESULT_MAX_BYTES = 1024 * 1024
#: Largest result whose (redacted) digest is computed when the value is not kept.
RESULT_DIGEST_MAX_BYTES = 64 * 1024

#: The ADR-0012 mutation classes, in ladder order.
MUTATION_CLASSES: tuple[str, ...] = (
    "none",
    "read-only",
    "idempotent-write",
    "non-idempotent-write",
    "external-side-effect",
    "unknown",
)


class NotJSONRepresentable(ValueError):
    """A value has no faithful JSON form; ``where`` names it for the user."""

    def __init__(self, where: str, why: str) -> None:
        super().__init__(f"{where} {why}")
        self.where = where
        self.why = why


def normalized_arg_hash(arguments: Any) -> str:
    """Order-insensitive digest of tool arguments (ADR-0300 D6).

    ``None`` is the same as ``{}`` (the MCP hook records ``arguments or {}``);
    any other value is hashed as itself -- a non-dict is never collapsed into
    ``{}``, which would make unrelated calls collide. ``replay._contract``
    re-exports this: one definition for capture and replay.
    """
    if arguments is None:
        arguments = {}
    canonical = json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def json_native(value: Any, where: str) -> Any:
    """Return *value* unchanged if it is JSON-native, else raise.

    JSON-native: exactly ``None``, ``bool``, ``int``, finite ``float``, ``str``,
    and ``list``/``dict`` (``str`` keys) of those -- subclasses excluded. A
    tuple, set, bytes, enum, non-``str`` key, non-finite float or any other
    object raises: JSON would change its type or cannot hold it, so serving it
    back would not be the recorded value.
    """
    kind = type(value)
    if value is None or kind in (bool, str, int):
        return value
    if kind is float:
        if not math.isfinite(value):
            raise NotJSONRepresentable(where, f"is the non-finite float {value!r}")
        return value
    if kind is list:
        for i, item in enumerate(value):
            json_native(item, f"{where}[{i}]")
        return value
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise NotJSONRepresentable(
                    where, f"has a non-str key of type {type(key).__name__}"
                )
            json_native(item, f"{where}[{key!r}]")
        return value
    if isinstance(value, tuple):
        raise NotJSONRepresentable(where, "is a tuple, which JSON would turn into a list")
    raise NotJSONRepresentable(where, f"is a {type(value).__name__}, not a JSON value")


def _argument_value(value: Any, where: str) -> Any:
    """Canonical form of one argument (D6): pydantic models and dataclasses are
    decoded from the live object, never from a type name; the rest must be
    JSON-native."""
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump) and not isinstance(value, type):
        try:
            return json_native(model_dump(mode="json"), where)
        except NotJSONRepresentable:
            raise
        except Exception as exc:  # noqa: BLE001 -- an odd model_dump is a refusal
            raise NotJSONRepresentable(where, f"could not be dumped: {exc}") from exc
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return json_native(dataclasses.asdict(value), where)
    return json_native(value, where)


@dataclass(frozen=True)
class CanonicalCall:
    """A call bound to its signature (D6), or why it could not be."""

    #: Canonical arguments, or ``None`` when ``not_canonical`` is set.
    arguments: dict[str, Any] | None
    #: Names the parameter that is not JSON-representable.
    not_canonical: str | None = None

    @property
    def digest(self) -> str | None:
        """The matching digest: over the arguments **after** secret redaction.

        See :func:`redacted_arguments_digest`.
        """
        return None if self.arguments is None else redacted_arguments_digest(self.arguments)

    @property
    def raw_digest(self) -> str | None:
        """ADR-0300's raw digest, for the matcher's exact tier only.

        Computed in memory at replay and never written anywhere: a digest of a raw
        value is a guessing oracle for a secret inside it (NF-166).
        """
        return None if self.arguments is None else normalized_arg_hash(self.arguments)


def redact_for_digest(value: Any) -> Any:
    """*value* with every ADR-0009 rule match masked as ``[REDACTED:<rule>]``.

    Same rule pack and placeholder as the capsule scanner
    (``secrets.redact_json_strings``). Every digest this module produces, and every
    digest replay matches on, is taken over this form: a digest of a raw value is an
    offline guessing oracle for a low-entropy secret inside it (the NF-166 lesson),
    and a detected secret must contribute only its placeholder. Applying it to an
    already-redacted value changes nothing, so capture and replay agree whether or
    not the capsule was sealed.
    """
    from novafabric.capture.secrets import redact_json_strings

    return redact_json_strings(value)


def redacted_arguments_digest(arguments: Any) -> str:
    """``normalized_arg_hash`` over :func:`redact_for_digest` of *arguments*.

    Used for the python surface on both sides: the payloads-off
    ``io.novafabric.arguments_digest`` at capture, and the live call and every
    stored record at replay (redact-then-hash, ADR-0306 D12.3, python surface).
    """
    return normalized_arg_hash(redact_for_digest(arguments))


def legacy_nested_reason(nested: int) -> str:
    """The not-servable reason slice-1 capture wrote for a boundary with nested records.

    Nesting alone no longer makes a record unservable (ADR-0306 slice 4): replay
    serves the boundary and consumes its nested records as *covered*. A capsule
    captured before that carries this exact text as its only reason; replay
    recognises it (``_contract.not_servable_reason``) so the capsule needs no
    re-capture. Do not change the text.
    """
    return (
        f"{nested} model/tool record(s) were written inside this boundary; "
        "serving it would leave them unrequested (nested boundaries are "
        "served from slice 3)"
    )


def echo_digest(value: Any) -> str:
    """``sha256:`` over the canonical JSON of a secret-redacted tool-result echo.

    Used by the D10 echo check on both sides -- the recorded request message and
    the replayed one -- so a secret the capsule scanner masked at seal compares
    equal to the raw value the live function returned. Values that are not JSON
    (an SDK message object) are converted with ``model_dump`` or ``str`` first;
    nothing is imported or evaluated.
    """
    return _digest(
        json.dumps(
            redact_for_digest(_plain(value)), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, default=str,
        )
    )


def _plain(value: Any, depth: int = 0) -> Any:
    """A JSON-like copy of *value*: SDK objects through ``model_dump(mode="json")``."""
    if depth > 32:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v, depth + 1) for v in value]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump) and not isinstance(value, type):
        try:
            return _plain(model_dump(mode="json", exclude_none=True), depth + 1)
        except Exception:  # noqa: BLE001 -- an odd object is compared by its str
            return str(value)
    return str(value)


def canonical_call(
    signature: inspect.Signature,
    ignore: frozenset[str],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> CanonicalCall:
    """Bind ``args``/``kwargs`` and canonicalise them (D6 rule 1).

    Raises :class:`TypeError` when the call does not bind -- the function would
    raise the same error before its body runs.
    """
    bound = signature.bind(*args, **kwargs)
    bound.apply_defaults()
    out: dict[str, Any] = {}
    for name, value in bound.arguments.items():
        if name in ignore:
            continue
        kind = signature.parameters[name].kind
        try:
            if kind is inspect.Parameter.VAR_POSITIONAL:
                out[name] = [
                    _argument_value(v, f"argument `{name}[{i}]`") for i, v in enumerate(value)
                ]
            elif kind is inspect.Parameter.VAR_KEYWORD:
                out[name] = {
                    str(k): _argument_value(v, f"argument `{name}[{k!r}]`")
                    for k, v in value.items()
                }
            else:
                out[name] = _argument_value(value, f"argument `{name}`")
        except NotJSONRepresentable as exc:
            return CanonicalCall(
                arguments=None,
                not_canonical=(
                    f"{exc.where} {exc.why}; it is not JSON-representable -- exclude it "
                    f"with ignore=({name!r},)"
                ),
            )
    return CanonicalCall(arguments=out)


def result_max_bytes() -> int:
    raw = os.environ.get(RESULT_MAX_BYTES_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RESULT_MAX_BYTES
    return value if value >= 0 else DEFAULT_RESULT_MAX_BYTES


@dataclass(frozen=True)
class EncodedResult:
    """What capture keeps of a returned value (D3)."""

    #: ``{"value": <JSON>}``, or ``None`` when the value is not kept.
    result: dict[str, Any] | None
    codec: str
    reason: str | None
    digest: str | None


def _digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def _redacted_digest(value: Any) -> str:
    """``sha256:`` over the canonical JSON of the secret-redacted value (NF-166)."""
    return _digest(
        json.dumps(
            redact_for_digest(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
    )


def encode_result(value: Any, *, keep_value: bool) -> EncodedResult:
    """Encode a tool's return value: JSON only, bounded by the size cap.

    A non-JSON-native value is recorded as not servable, with why. The digest is
    taken over the **secret-redacted** value, never the raw one, and only where it
    stands in for a value that is not kept -- payload capture off. When the value
    is kept it is the evidence (and the capsule scanner redacts it at seal), so no
    digest is computed; over the size cap neither is kept, because redacting an
    unbounded result just to digest it is unbounded work in the workload.
    """
    try:
        json_native(value, "the result")
    except NotJSONRepresentable as exc:
        return EncodedResult(
            result=None, codec=CODEC_NOT_SERVABLE,
            reason=f"{exc.where} {exc.why} (slice 1 serves JSON-native results only)",
            digest=None,
        )
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    cap = result_max_bytes()
    size = len(text.encode())
    if size > cap:
        return EncodedResult(
            result=None, codec=CODEC_NOT_SERVABLE,
            reason=(
                f"the result is {size} bytes, over the {cap}-byte cap "
                f"({RESULT_MAX_BYTES_ENV}); neither the value nor a digest was kept"
            ),
            digest=None,
        )
    if not keep_value:
        # Redaction costs ~150 us per KiB in the workload (the ADR-0009 pack), so
        # the stand-in digest is bounded: a larger result keeps no digest at all.
        digest = _redacted_digest(value) if size <= RESULT_DIGEST_MAX_BYTES else None
        return EncodedResult(result=None, codec=CODEC_NOT_SERVABLE, reason=None, digest=digest)
    return EncodedResult(result={"value": value}, codec=CODEC_JSON, reason=None, digest=None)


def decode_result(record: dict[str, Any]) -> tuple[bool, Any]:
    """``(True, value)`` for a servable success record, else ``(False, None)``.

    Never imports or evaluates anything: the value is the JSON the record holds.
    """
    ext = record.get("extensions")
    if not isinstance(ext, dict) or ext.get(RESULT_CODEC_EXT) != CODEC_JSON:
        return False, None
    result = record.get("result")
    if not isinstance(result, dict) or "value" not in result:
        return False, None
    return True, result["value"]
