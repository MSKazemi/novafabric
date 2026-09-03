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

"""The NF-161/162/163 facets are additive *against the real schema* (ADR-0148 I-1).

Each facet module already tests ``attach_facet`` against a plain ``dict``, which proves
the shape it writes and nothing about whether a capsule carrying it still validates.
``facets`` is ``additionalProperties: false`` (ADR-0196 D2), so those two questions have
different answers, and only this file asks the second one. It is the same span that was
found open on 2026-09-03 holding 13 unregistered facet names — see
``tests/docs/test_capsule_facet_contract.py``.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import jsonschema
import pytest

from novafabric.trust.provenance import c2pa_bind, watermark
from trust._provenance_fixtures import IMAGE_BYTES, a_capsule, a_manifest

SCHEMA_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "novafabric"
    / "schemas"
    / "run-capsule.schema.json"
)

#: A capsule manifest the capture path actually wrote and ``nova validate`` accepted,
#: with its machine-identifying fields replaced. Derived from a real run rather than
#: hand-written, because a hand-written base can satisfy ``required`` while violating an
#: enum or a ``$ref`` — which is exactly what the first draft of this file did, and what
#: :func:`test_the_base_capsule_is_valid_before_any_facet` caught.
BASE_CAPSULE_PATH = pathlib.Path(__file__).with_name("_capsule_base.json")


def base_capsule() -> dict[str, Any]:
    """A fresh copy of the base capsule, so no test can mutate another's input."""
    return json.loads(BASE_CAPSULE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator() -> jsonschema.protocols.Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def test_the_base_capsule_is_valid_before_any_facet(
    validator: jsonschema.protocols.Validator,
) -> None:
    """Without this, every assertion below could pass for the wrong reason."""
    validator.validate(base_capsule())


def test_a_capsule_with_no_facets_key_at_all_is_valid(
    validator: jsonschema.protocols.Validator,
) -> None:
    """I-1 — a capsule captured before this feature existed is unchanged and valid."""
    capsule = base_capsule()
    assert "facets" not in capsule
    validator.validate(capsule)


def test_a_capsule_carrying_the_media_provenance_facet_validates(
    tmp_path: pathlib.Path, validator: jsonschema.protocols.Validator
) -> None:
    """The facet the NF-161 writer actually produces, against the real schema."""
    capsule_dir = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    facet = c2pa_bind.build_facet(capsule_dir)
    assert facet is not None

    capsule = base_capsule()
    c2pa_bind.attach_facet(capsule, facet)
    validator.validate(capsule)


def test_a_capsule_carrying_the_watermark_facet_validates(
    tmp_path: pathlib.Path, validator: jsonschema.protocols.Validator
) -> None:
    doc = a_manifest()
    doc["manifests"]["urn:manifest:1"]["assertions"].append(
        {"label": watermark.SOFT_BINDING_LABEL, "data": {"present": True}}
    )
    capsule_dir = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=doc)
    facet = watermark.build_facet(capsule_dir)
    assert facet is not None

    capsule = base_capsule()
    watermark.attach_facet(capsule, facet)
    validator.validate(capsule)


def test_both_facets_coexist_on_one_capsule(
    tmp_path: pathlib.Path, validator: jsonschema.protocols.Validator
) -> None:
    doc = a_manifest()
    doc["manifests"]["urn:manifest:1"]["assertions"].append(
        {"label": watermark.SOFT_BINDING_LABEL, "data": {"present": False}}
    )
    capsule_dir = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=doc)
    media = c2pa_bind.build_facet(capsule_dir)
    marks = watermark.build_facet(capsule_dir)
    assert media is not None and marks is not None

    capsule = base_capsule()
    c2pa_bind.attach_facet(capsule, media)
    watermark.attach_facet(capsule, marks)
    validator.validate(capsule)
    assert set(capsule["facets"]) == {"media_provenance", "watermark_presence"}


def test_an_unregistered_facet_name_really_is_rejected(
    validator: jsonschema.protocols.Validator,
) -> None:
    """Proves the two tests above are not passing because the registry is open.

    If ``facets`` accepted anything, every 'the facet validates' assertion here would be
    vacuous. This is the assertion that makes them mean something.
    """
    capsule = base_capsule()
    capsule["facets"] = {"definitely_not_a_registered_facet": {"x": 1}}
    with pytest.raises(jsonschema.ValidationError, match="Additional properties"):
        validator.validate(capsule)


def test_the_facets_registry_is_closed_by_design() -> None:
    """ADR-0196 D2. If this ever flips to open, the guard above stops meaning anything."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["facets"]["additionalProperties"] is False
