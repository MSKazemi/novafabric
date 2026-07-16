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

"""Score-configuration record — the ADR-0117 ``ScoreConfig`` model and validation hook.

A ``ScoreConfig`` is a **named, reusable, immutable, content-addressed definition** of
what a valid :class:`~novafabric.eval.scores.Score` for a given ``name`` looks like:
its ``value_type`` (reusing :class:`~novafabric.eval.scores.ScoreValueType` — no new
enum), and, per type, the allowed ``categories`` (optionally ordinal-ranked) or the
admissible numeric ``range`` with a preferred ``direction``. Configs make scores under
one ``name`` coherent and comparable across capsules: an aggregate can pin the exact
``content_digest`` it was computed against (ADR-0117 D4).

Wire contract: ``schemas/score-config-v0.schema.json``. Normative constraints C1–C5
(design/spec/score-config-v0.md) are enforced by the model validator here.

The catalog (SQLite storage, version bumping, resolution) lives in
:mod:`novafabric.eval.score_config_catalog`. This module is pure data + stdlib hashing:
no IO, no new dependency.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from novafabric.capture._ulid import new_ulid
from novafabric.eval.scores import Score, ScoreValueType

SCORE_CONFIG_SCHEMA_VERSION = "0.1.0"

_ULID_RE = re.compile(r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

#: Body fields excluded from the canonical (digested) definition body — these are
#: envelope/derived, not part of the definition (spec §Data model).
_ENVELOPE_FIELDS = frozenset({"config_id", "content_digest", "created_at"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScoreConfigError(Exception):
    """Base class for score-configuration errors."""


class ScoreConfigViolation(ScoreConfigError):
    """A ``Score`` does not satisfy the ``ScoreConfig`` governing its name (D2).

    Raised by :func:`validate_score_against_config`; when raised on the append
    path, the score MUST NOT be appended.
    """


class ScoreDirection(str, Enum):
    """Preferred direction of a numeric metric (informative, for aggregation)."""

    HIGHER_BETTER = "higher-better"
    LOWER_BETTER = "lower-better"


class ScoreCategory(BaseModel):
    """One allowed categorical value, with an optional ordinal rank."""

    model_config = ConfigDict(extra="forbid")

    value: str
    ordinal: int | None = None


class ScoreRange(BaseModel):
    """Inclusive numeric bounds ``[min, max]`` plus an optional direction."""

    model_config = ConfigDict(extra="forbid")

    min: float
    max: float
    direction: ScoreDirection | None = None

    @model_validator(mode="after")
    def _check(self) -> ScoreRange:
        if self.min > self.max:
            raise ValueError(f"range min ({self.min}) must not exceed max ({self.max})")
        return self


class ScoreConfig(BaseModel):
    """A named, immutable, content-addressed score definition (one catalog entry).

    ``content_digest`` may be omitted at construction — it is then derived from the
    canonical definition body. When supplied (e.g. parsing a stored record), it MUST
    equal the recomputed digest (C5), so a tampered body is rejected at parse time.
    """

    model_config = ConfigDict(extra="forbid")

    config_id: str = Field(default_factory=new_ulid)
    name: str = Field(min_length=1)
    value_type: ScoreValueType
    description: str
    categories: list[ScoreCategory] | None = None
    range: ScoreRange | None = None
    labels: list[str] | None = None
    extensions: dict[str, Any] | None = None
    version: int = Field(default=1, ge=1)
    content_digest: str = ""
    created_at: str = Field(default_factory=_now_iso)

    @model_validator(mode="after")
    def _check(self) -> ScoreConfig:
        if not _ULID_RE.match(self.config_id):
            raise ValueError(f"config_id is not a valid ULID: {self.config_id!r}")
        # C1–C3: value_type ⇒ shape agreement.
        if self.value_type is ScoreValueType.BOOLEAN and (
            self.categories is not None or self.range is not None
        ):
            raise ValueError("value_type 'boolean' must carry neither categories nor range (C1)")
        if self.value_type is ScoreValueType.CATEGORICAL:
            if not self.categories:
                raise ValueError("value_type 'categorical' requires non-empty categories (C2)")
            if self.range is not None:
                raise ValueError("value_type 'categorical' must not carry a range (C2)")
            values = [c.value for c in self.categories]
            if len(values) != len(set(values)):
                raise ValueError("categories[].value must be unique (C2)")
        if self.value_type is ScoreValueType.NUMERIC:
            if self.range is None:
                raise ValueError("value_type 'numeric' requires a range (C3)")
            if self.categories is not None:
                raise ValueError("value_type 'numeric' must not carry categories (C3)")
        # C5: digest is derived from the canonical body; a supplied digest must match.
        derived = config_digest(self)
        if not self.content_digest:
            self.content_digest = derived
        elif self.content_digest != derived:
            raise ValueError(
                f"content_digest does not match the canonical body: "
                f"got {self.content_digest!r}, derived {derived!r}"
            )
        return self


def canonical_config_bytes(config: ScoreConfig) -> bytes:
    """Canonical-JSON bytes of the *definition body* (the digested content).

    Deterministic: keys sorted, no insignificant whitespace, ``None`` fields dropped,
    enums rendered as their string values. Envelope fields (``config_id``,
    ``content_digest``, ``created_at``) are excluded — equal bodies ⇒ equal bytes,
    whoever and whenever registered them.
    """
    payload = config.model_dump(mode="json", exclude=set(_ENVELOPE_FIELDS), exclude_none=True)
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def config_digest(config: ScoreConfig) -> str:
    """Return the ``sha256:<hex>`` content digest of *config*'s definition body (C5)."""
    return "sha256:" + hashlib.sha256(canonical_config_bytes(config)).hexdigest()


def validate_score_against_config(score: Score, config: ScoreConfig) -> None:
    """Check that *score* satisfies *config*; raise :class:`ScoreConfigViolation` if not.

    Implements ADR-0117 D2: ``value_type`` agreement, category membership, and
    inclusive numeric bounds. Callers resolve the config by ``score.name`` first —
    a name mismatch here is a usage error and also raises.
    """
    ref = f"{config.name}@{config.version} ({config.content_digest})"
    if score.name != config.name:
        raise ScoreConfigViolation(
            f"score name {score.name!r} does not match config name {config.name!r}"
        )
    if score.value_type is not config.value_type:
        raise ScoreConfigViolation(
            f"score value_type {score.value_type.value!r} disagrees with config "
            f"{ref}: expected {config.value_type.value!r}"
        )
    if config.value_type is ScoreValueType.CATEGORICAL:
        allowed = [c.value for c in config.categories or []]
        if score.value not in allowed:
            raise ScoreConfigViolation(
                f"score value {score.value!r} is not an allowed category of config "
                f"{ref}: expected one of {allowed}"
            )
    elif config.value_type is ScoreValueType.NUMERIC:
        rng = config.range
        assert rng is not None  # guaranteed by C3
        value = float(score.value)  # numeric per the value_type agreement above
        if not (rng.min <= value <= rng.max):
            raise ScoreConfigViolation(
                f"score value {value} is outside the range [{rng.min}, {rng.max}] "
                f"of config {ref}"
            )
    # BOOLEAN: value_type agreement already guarantees a bool value (Score invariant).
