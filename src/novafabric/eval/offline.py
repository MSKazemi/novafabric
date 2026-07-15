# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Trace-first zero-token offline eval (NF-009, ADR-0099).

Structural / contract / metamorphic assertions over an **already-stored** capsule.
Because the capsule is on disk, the marginal cost is near zero — these checks run with
**zero model calls** (requirement 6) and each emits a ``Score`` with ``source='code'``.

- :func:`run_coverage` — did the run exercise every declared tool? (over ``tool-calls.jsonl``)
- :func:`run_contract` — do recorded outputs satisfy a JSON-schema contract?
- :func:`run_metamorphic` — does a recorded input transform preserve a recorded invariant?
- :func:`run_metamorphic_spec` — the same, driven by a declarative YAML/dict check-spec
  (``schemas/features/metamorphic-check-v0.schema.json``): records whose *input* collapses
  to the same value under a named ``transform`` form metamorphic pairs, and every pair's
  *output* must satisfy a named ``invariant``. This is the ``--spec`` surface of
  ``nova eval offline --check metamorphic``.

Each check is a pure function: read-only over the capsule directory, deterministic, no IO
beyond reading the capsule. The subject of every emitted score is the capsule Merkle root,
so the score is bound to the exact capsule content.
"""

from __future__ import annotations

import json
import string
from pathlib import Path
from typing import Any, Callable, Sequence

import jsonschema  # type: ignore[import-untyped]

from novafabric.eval.card import EvalCard, card_digest
from novafabric.eval.scores import Score, ScoreSource, ScoreValueType
from novafabric.evidence.merkle import capsule_merkle_root

# Built-in ``code`` evaluator cards for the offline checks. They carry no signature
# (code scorers need none to produce a valid Score); their digest is the reproducibility
# key stamped onto each emitted score.
COVERAGE_CARD = EvalCard(
    card_id="nf-offline-coverage", name="Tool Coverage", version="1.0.0", source=ScoreSource.CODE
)
CONTRACT_CARD = EvalCard(
    card_id="nf-offline-contract", name="Output Contract", version="1.0.0", source=ScoreSource.CODE
)
METAMORPHIC_CARD = EvalCard(
    card_id="nf-offline-metamorphic", name="Metamorphic", version="1.0.0", source=ScoreSource.CODE
)


def _capsule_subject(capsule_dir: Path) -> str:
    return capsule_merkle_root(capsule_dir)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _numeric_score(
    capsule_dir: Path, card: EvalCard, name: str, value: float
) -> Score:
    return Score(
        subject=_capsule_subject(capsule_dir),
        subject_kind="capsule",
        name=name,
        value=value,
        value_type=ScoreValueType.NUMERIC,
        source=ScoreSource.CODE,
        evaluator_id=card.card_id,
        eval_card_digest=card_digest(card),
    )


def run_coverage(
    capsule_dir: str | Path, declared_tools: Sequence[str], name: str = "tool_coverage"
) -> Score:
    """Fraction of *declared_tools* that were exercised in the capsule (zero-token).

    Reads ``tool-calls.jsonl`` and collects distinct ``tool_name`` values. With an empty
    ``declared_tools`` the coverage is vacuously ``1.0``.
    """
    capsule_dir = Path(capsule_dir)
    exercised = {
        rec.get("tool_name")
        for rec in _read_jsonl(capsule_dir / "tool-calls.jsonl")
        if rec.get("tool_name") is not None
    }
    declared = list(declared_tools)
    if not declared:
        fraction = 1.0
    else:
        covered = sum(1 for tool in declared if tool in exercised)
        fraction = covered / len(declared)
    return _numeric_score(capsule_dir, COVERAGE_CARD, name, fraction)


def run_contract(
    capsule_dir: str | Path,
    schema: dict[str, Any],
    field: str = "output",
    records_file: str = "tool-calls.jsonl",
    name: str = "output_contract",
) -> Score:
    """Fraction of records whose ``field`` satisfies *schema* (zero-token).

    A record missing ``field`` counts as a contract failure. With no records the score is
    vacuously ``1.0``. Validation uses the bundled ``jsonschema`` (no model call).
    """
    capsule_dir = Path(capsule_dir)
    records = _read_jsonl(capsule_dir / records_file)
    if not records:
        return _numeric_score(capsule_dir, CONTRACT_CARD, name, 1.0)
    validator = jsonschema.Draft202012Validator(schema)
    valid = 0
    for rec in records:
        if field not in rec:
            continue
        if validator.is_valid(rec[field]):
            valid += 1
    return _numeric_score(capsule_dir, CONTRACT_CARD, name, valid / len(records))


def run_metamorphic(
    capsule_dir: str | Path,
    pairs: Sequence[tuple[Any, Any]],
    invariant: Callable[[Any, Any], bool],
    name: str = "metamorphic",
) -> Score:
    """Boolean: does *invariant* hold for every recorded ``(input, output)`` *pair*?

    Programmatic NF-009 metamorphic check (the YAML check-spec CLI is deferred). With no
    pairs the invariant vacuously holds.
    """
    capsule_dir = Path(capsule_dir)
    holds = all(invariant(a, b) for a, b in pairs)
    return Score(
        subject=_capsule_subject(capsule_dir),
        subject_kind="capsule",
        name=name,
        value=holds,
        value_type=ScoreValueType.BOOLEAN,
        source=ScoreSource.CODE,
        evaluator_id=METAMORPHIC_CARD.card_id,
        eval_card_digest=card_digest(METAMORPHIC_CARD),
    )


# ---------------------------------------------------------------------------
# Declarative metamorphic check-spec (NF-009 ``--spec`` surface)
# ---------------------------------------------------------------------------


class MetamorphicSpecError(ValueError):
    """A metamorphic check-spec is malformed or names an unknown transform/invariant."""


def _t_collapse_whitespace(s: str) -> str:
    return " ".join(s.split())


def _t_remove_punctuation(s: str) -> str:
    return s.translate(str.maketrans("", "", string.punctuation))


# Named input transforms used to derive a pairing key. Composed left-to-right.
_TRANSFORMS: dict[str, Callable[[str], str]] = {
    "identity": lambda s: s,
    "lower": str.lower,
    "strip": str.strip,
    "collapse_whitespace": _t_collapse_whitespace,
    "remove_punctuation": _t_remove_punctuation,
}


def _normalize_text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return _t_collapse_whitespace(text.strip().lower())


def _make_invariant(name: str, tolerance: float) -> Callable[[Any, Any], bool]:
    if name == "equal":
        return lambda a, b: a == b
    if name == "equal_normalized":
        return lambda a, b: _normalize_text(a) == _normalize_text(b)
    if name == "numeric_close":

        def _close(a: Any, b: Any) -> bool:
            try:
                return abs(float(a) - float(b)) <= tolerance
            except (TypeError, ValueError):
                return False

        return _close
    if name == "length_within":
        return lambda a, b: abs(len(str(a)) - len(str(b))) <= tolerance
    raise MetamorphicSpecError(f"unknown invariant: {name!r}")


def _compose_transform(names: Sequence[str]) -> Callable[[Any], str]:
    funcs: list[Callable[[str], str]] = []
    for raw in names:
        try:
            funcs.append(_TRANSFORMS[raw])
        except KeyError as exc:  # pragma: no cover - defensive
            raise MetamorphicSpecError(f"unknown transform: {raw!r}") from exc

    def _apply(value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
        for fn in funcs:
            text = fn(text)
        return text

    return _apply


def run_metamorphic_spec(
    capsule_dir: str | Path, spec: dict[str, Any], name: str | None = None
) -> Score:
    """Run a declarative NF-009 metamorphic check over a stored capsule (zero-token).

    *spec* is a ``metamorphic-check-v0`` object (see
    ``schemas/features/metamorphic-check-v0.schema.json``). Records read from
    ``spec['records_file']`` (default ``tool-calls.jsonl``) are grouped by
    ``transform(record[input_field])``; within each group of ≥2 records, consecutive
    ``output_field`` values must satisfy ``invariant``. Records missing either field are
    skipped. With no metamorphic pairs the invariant vacuously holds (``True``).

    Raises :class:`MetamorphicSpecError` on a missing/unknown ``transform`` or ``invariant``.
    """
    capsule_dir = Path(capsule_dir)
    if "transform" not in spec or "invariant" not in spec:
        raise MetamorphicSpecError("check-spec requires 'transform' and 'invariant'")

    transform_raw = spec["transform"]
    transform_names = [transform_raw] if isinstance(transform_raw, str) else list(transform_raw)
    if not transform_names:
        raise MetamorphicSpecError("'transform' must name at least one transform")
    transform = _compose_transform(transform_names)

    tolerance = float(spec.get("tolerance", 0) or 0)
    invariant = _make_invariant(str(spec["invariant"]), tolerance)

    input_field = str(spec.get("input_field", "input"))
    output_field = str(spec.get("output_field", "output"))
    records_file = str(spec.get("records_file", "tool-calls.jsonl"))

    records = _read_jsonl(capsule_dir / records_file)
    groups: dict[str, list[Any]] = {}
    for rec in records:
        if input_field not in rec or output_field not in rec:
            continue
        key = transform(rec[input_field])
        groups.setdefault(key, []).append(rec[output_field])

    pairs: list[tuple[Any, Any]] = []
    for outputs in groups.values():
        for prev, cur in zip(outputs, outputs[1:]):
            pairs.append((prev, cur))

    return run_metamorphic(
        capsule_dir, pairs, invariant, name=name or str(spec.get("name") or "metamorphic")
    )
