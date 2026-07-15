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

"""Validate outer-envelope payloads against vendored standard schemas (ADR-0096).

The schemas under ``envelopes/_schemas/`` capture the required-field contract of the
in-toto Statement v1 and SLSA Provenance v1 specs. Emitters can validate their output so a
structural drift (a renamed or missing field) fails fast instead of producing an envelope
that a stock in-toto/SLSA verifier would reject.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema  # type: ignore[import-untyped]

_SCHEMA_DIR = Path(__file__).parent / "_schemas"

INTOTO_STATEMENT_SCHEMA = "intoto-statement-v1.schema.json"
SLSA_PROVENANCE_SCHEMA = "slsa-provenance-v1.schema.json"


class EnvelopeSchemaError(ValueError):
    """Raised when an envelope payload does not match its vendored standard schema."""


@lru_cache(maxsize=None)
def _load_schema(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))
    return data


def _validate(instance: Any, schema_name: str) -> None:
    try:
        jsonschema.validate(instance=instance, schema=_load_schema(schema_name))
    except jsonschema.ValidationError as exc:
        raise EnvelopeSchemaError(f"{schema_name}: {exc.message}") from exc


def validate_intoto_statement(statement: dict[str, Any]) -> None:
    """Validate an in-toto Statement v1 (raises :class:`EnvelopeSchemaError`)."""
    _validate(statement, INTOTO_STATEMENT_SCHEMA)


def validate_slsa_provenance(statement: dict[str, Any]) -> None:
    """Validate a SLSA-provenance in-toto Statement: outer Statement + inner predicate."""
    _validate(statement, INTOTO_STATEMENT_SCHEMA)
    _validate(statement.get("predicate", {}), SLSA_PROVENANCE_SCHEMA)
