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
"""The generated architecture flow diagrams stay in sync, accessible and honest.

The four server-side SVGs and the explainer's ``FLOWS`` block come from one data
model in ``scripts/gen_architecture_flows.py``. This guards three things: the
committed outputs match the generator, every diagram keeps the accessibility and
reduced-motion contract, and every step names code that exists.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "gen_architecture_flows.py"
ARCH = REPO / "docs" / "architecture"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="generator not present")


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_architecture_flows", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_architecture_flows"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_generated_outputs_are_current(gen) -> None:
    stale = [
        str(path.relative_to(REPO))
        for path, text in gen.outputs().items()
        if not path.exists() or path.read_text(encoding="utf-8") != text
    ]
    assert not stale, f"stale; run: python scripts/gen_architecture_flows.py -> {stale}"


def test_each_flow_has_a_page_linked_from_the_index(gen) -> None:
    index = (ARCH / "README.md").read_text(encoding="utf-8")
    for flow in gen.FLOWS:
        page = ARCH / flow.page
        assert page.is_file(), flow.page
        assert f"]({flow.page})" in index, f"{flow.page} missing from the README index"
        assert f"{flow.id}.svg" in page.read_text(encoding="utf-8")
        assert f"{flow.id}.svg" in index


def test_svgs_are_accessible_themed_and_respect_reduced_motion(gen) -> None:
    for flow in gen.FLOWS:
        svg = (REPO / "docs" / "assets" / "architecture" / f"{flow.id}.svg").read_text(
            encoding="utf-8"
        )
        assert 'role="img"' in svg and '<title id="t">' in svg and '<desc id="d">' in svg
        assert "<script" not in svg
        assert "http://" not in svg.replace("http://www.w3.org/2000/svg", "")
        assert "prefers-color-scheme:light" in svg
        assert "prefers-reduced-motion:reduce" in svg
        # Every step is also present as always-visible text.
        for step in flow.steps:
            assert step.caption.replace("&", "&amp;") in svg


def test_flow_steps_cite_existing_modules_and_label_maturity(gen) -> None:
    for flow in gen.FLOWS:
        for i, step in enumerate(flow.steps, 1):
            assert step.mat in {"works today", "experimental", "planned", "future design"}
            for token in re.findall(r"[\w/]+\.py", step.ref):
                hits = [
                    h
                    for h in (REPO / "src" / "novafabric").rglob(Path(token).name)
                    if str(h).endswith(token)
                ]
                assert hits, f"{flow.id} step {i}: {token} not found under src/novafabric"


def test_explainer_is_self_contained() -> None:
    html = (ARCH / "explainer.html").read_text(encoding="utf-8")
    assert not re.search(r"<script[^>]+src=", html), "explainer must not load scripts"
    assert "https://cdn" not in html
    assert "prefers-reduced-motion" in html
