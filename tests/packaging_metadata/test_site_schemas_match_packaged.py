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

"""The website's vendored schemas are the third copy, and nothing was watching it.

``schemas/`` (canonical, OAS v1.0 target) and ``src/novafabric/schemas/`` (what
an installed CLI validates against) are held in step by
``test_packaged_schemas_match_canonical.py``. There is a **third** copy —
``web/src/data/schemas/`` — imported by ``web/src/lib/fixtures.ts`` and compiled
by ``web/src/lib/validate.ts`` into the validator the showcase site runs at build
time. It was outside every guard.

The site's own Spec page (``web/src/pages/spec.astro``) states:

    The showcase site validates its fixture against the same schemas at build
    time, so the demo can never silently drift from the real format.

On 2026-08-28 that was not true. Seven of the ten vendored copies were stale,
between them omitting **27 properties** the packaged schemas declare — including
``facets``, the project's headline extension point, plus ``session_id``,
``sequence``, ``usage_totals`` and ``evidence_digests``. Because these schemas are
``additionalProperties: false``, a capsule that ``nova validate`` accepts today
was *rejected* by the site's own validator. The drift the sentence promises cannot
happen had already happened, in the direction that makes the site understate the
format.

Two invariants, because the claim has two halves:

* the vendored copies are byte-identical to the packaged ones (same schemas), and
* every showcase capsule fixture validates against them (validated at build time).

Byte-identity is deliberate rather than a property-by-property comparison. Unlike
the canonical/packaged pair — where the v1-target-vs-in-force split is a real
generational difference — the website has no reason to carry a *different*
schema from the one the CLI enforces. Whatever the CLI accepts is what the
showcase must demonstrate.

Fix when the first test fails::

    cp src/novafabric/schemas/<name>.schema.json web/src/data/schemas/

⚠ The vendored JSON is imported at *build* time, so the compiled bundle under
``src/novafabric/serve/static/_astro/`` keeps whatever it was built with. Syncing
these files does not update the shipped dashboard until the site is rebuilt.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO = Path(__file__).resolve().parents[2]
SITE_SCHEMAS = REPO / "web" / "src" / "data" / "schemas"
PACKAGED_SCHEMAS = REPO / "src" / "novafabric" / "schemas"
SITE_CAPSULES = REPO / "web" / "src" / "data" / "fixtures" / "capsules"

_VENDORED = sorted(p.name for p in SITE_SCHEMAS.glob("*.json"))
_FIXTURES = sorted(p.name for p in SITE_CAPSULES.iterdir() if (p / "capsule.json").is_file())


def test_there_is_something_to_compare() -> None:
    """Guard the guard: a wrong path would make every case below vacuous."""
    assert len(_VENDORED) >= 10, _VENDORED
    assert len(_FIXTURES) >= 3, _FIXTURES
    assert PACKAGED_SCHEMAS.is_dir()


@pytest.mark.parametrize("name", _VENDORED)
def test_site_schema_is_the_packaged_schema(name: str) -> None:
    packaged = PACKAGED_SCHEMAS / name
    assert packaged.is_file(), (
        f"web/src/data/schemas/{name} has no counterpart under "
        "src/novafabric/schemas/. The showcase must not demonstrate a format the "
        "installed CLI does not enforce."
    )
    site_text = (SITE_SCHEMAS / name).read_bytes()
    if site_text == packaged.read_bytes():
        return

    site = json.loads(site_text)
    pkg = json.loads(packaged.read_text())
    missing = sorted(set(pkg.get("properties", {})) - set(site.get("properties", {})))
    pytest.fail(
        f"web/src/data/schemas/{name} has drifted from the packaged schema"
        + (f"; it omits {missing}" if missing else "")
        + ". These schemas are additionalProperties:false, so the showcase site "
        "rejects capsules `nova validate` accepts, and the Spec page's promise "
        '("validates its fixture against the same schemas") is false.\n'
        f"fix: cp src/novafabric/schemas/{name} web/src/data/schemas/"
    )


@pytest.mark.parametrize("run", _FIXTURES)
def test_site_capsule_fixture_validates_against_the_shipped_schema(run: str) -> None:
    """The Spec page's claim, enforced here rather than trusted to the JS build."""
    schema = json.loads((SITE_SCHEMAS / "run-capsule.schema.json").read_text())
    capsule = json.loads((SITE_CAPSULES / run / "capsule.json").read_text())
    errors = sorted(
        Draft202012Validator(schema).iter_errors(capsule),
        key=lambda e: list(e.path),
    )
    assert not errors, (
        f"the showcase capsule fixture {run} does not satisfy the schema the "
        "site publishes as the real format:\n  "
        + "\n  ".join(f"{list(e.path)}: {e.message}" for e in errors[:6])
    )
