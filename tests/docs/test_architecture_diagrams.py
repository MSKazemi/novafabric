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
"""The generated architecture diagrams and the explainer stay in sync, accessible and honest.

The flow SVGs, ``how-it-works.svg`` and the explainer's ``FLOWS`` and ``STORY``
blocks come from one data model in ``scripts/gen_architecture_flows.py``. This
guards: the committed outputs match the generator; every diagram keeps the
accessibility and reduced-motion contract; every step names code that exists and
carries a maturity label; the end-to-end story covers every stage and never puts
unbuilt work in a built stage; the explainer stays self-contained; and every deep
link into it resolves.
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


def _diagrams(gen):
    return (*gen.FLOWS, gen.STORY)


def test_each_flow_has_a_page_linked_from_the_index(gen) -> None:
    index = (ARCH / "README.md").read_text(encoding="utf-8")
    for flow in gen.FLOWS:
        page = ARCH / flow.page
        assert page.is_file(), flow.page
        assert f"]({flow.page})" in index, f"{flow.page} missing from the README index"
        assert f"{flow.id}.svg" in index, f"{flow.id}.svg missing from the diagram index"
        # A server flow has its own page, which embeds the SVG. A pipeline flow zooms
        # into one stage of a page that predates it; the explainer and the diagram
        # index carry it until that page embeds it too.
        if flow.group == "server":
            assert f"{flow.id}.svg" in page.read_text(encoding="utf-8")
        else:
            assert flow.group == "pipeline", flow.id
    assert f"{gen.STORY.id}.svg" in index, "how-it-works.svg missing from the diagram index"


def test_svgs_are_accessible_themed_and_respect_reduced_motion(gen) -> None:
    for flow in _diagrams(gen):
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
    for flow in _diagrams(gen):
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
    assert not re.search(r"<link[^>]+href=", html), "explainer must not load stylesheets"
    assert "@import" not in html, "no CSS imports"
    # url(#marker) points inside the page; any other url() would fetch something.
    assert not re.search(r"url\((?!#)", html), "no fetched CSS or SVG assets"
    # The only absolute URL allowed is the SVG namespace the scripts create nodes in.
    urls = set(re.findall(r"(?:https?:)?//[\w.-]+\.[a-z]{2,}[^\s\"'<)]*", html))
    assert urls <= {"http://www.w3.org/2000/svg"}, f"external URLs in the explainer: {urls}"
    assert "prefers-reduced-motion" in html


REQUIRED_STAGES = {
    "workload", "capture", "capsule", "seal", "registry", "replay", "diff", "verify", "server",
}


def test_story_covers_every_stage_of_the_system(gen) -> None:
    keys = [s.key for s in gen.STAGES]
    assert REQUIRED_STAGES <= set(keys), REQUIRED_STAGES - set(keys)
    used = {s.stage for s in gen.STORY.steps}
    assert used == set(keys), f"stages with no step: {set(keys) - used}"
    # Steps run in stage order, so the stage bar reads left to right as the story plays.
    order = [keys.index(s.stage) for s in gen.STORY.steps]
    assert order == sorted(order)
    html = (ARCH / "explainer.html").read_text(encoding="utf-8")
    for key in keys:
        assert f'"stage": "{key}"' in html, key


def test_story_never_presents_unbuilt_work_as_built(gen) -> None:
    unbuilt = {"planned", "future design"}
    for i, s in enumerate(gen.STORY.steps, 1):
        if s.mat in unbuilt:
            assert s.stage == "next", f"story step {i} ({s.mat}) sits in built stage {s.stage}"
            assert s.caption.lower().startswith(s.mat), f"story step {i} must lead with {s.mat}"
            assert not s.unreleased
        else:
            assert s.stage != "next", f"story step {i} is built but sits in 'Not built yet'"
    planned = [n.id for n in gen.STORY.nodes if n.style == "planned"]
    assert planned == ["next"]
    assert {s.mat for s in gen.STORY.steps} == set(gen.MATURITY), "all four labels appear"


def test_unreleased_marker_names_the_latest_release(gen) -> None:
    # Steps flagged ``unreleased`` describe main after LAST_RELEASE. When a release is
    # cut, this fails until the generator is told, so a released feature is never
    # still labelled "unreleased" (and an unreleased one never reads as shipped).
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    latest = re.search(r"^## \[(\d+\.\d+\.\d+)\]", changelog, re.M)
    assert latest, "no released version in CHANGELOG.md"
    assert gen.LAST_RELEASE == f"v{latest.group(1)}", (
        f"CHANGELOG's latest release is v{latest.group(1)}: update LAST_RELEASE in "
        "scripts/gen_architecture_flows.py and clear the unreleased flags it now covers"
    )


STORY_CONTROLS = (
    "story", "stages", "ssvg", "sfirst", "sprev", "splay", "snext", "slast", "sscrub",
    "sspeed", "scaption", "slist",
)


def test_explainer_story_player_contract() -> None:
    html = (ARCH / "explainer.html").read_text(encoding="utf-8")
    for el in STORY_CONTROLS:
        assert f'id="{el}"' in html, f"story control #{el} missing"
    start = html.index("/* BEGIN generated story")
    player = html[start: html.index("</script>", start)]
    assert "prefers-reduced-motion: reduce" in player, "the story must honour reduced motion"
    assert "keydown" in player and "hashchange" in player
    # Captions are announced when a reader steps, and muted while it plays.
    assert 'setAttribute("aria-live", playing ? "off" : "polite")' in player


def _old_walkthrough_steps(html: str) -> int:
    block = html[html.index("var STEPS = ["): html.index("var $ = function", html.index("var STEPS"))]
    return len(re.findall(r"\{ p: \d", block))


def test_explainer_deep_links_resolve(gen) -> None:
    html = (ARCH / "explainer.html").read_text(encoding="utf-8")
    index = (ARCH / "README.md").read_text(encoding="utf-8")
    flows = {f.id: len(f.steps) for f in gen.FLOWS}
    stages = {s.key for s in gen.STAGES}
    links = re.findall(r'href=\\?"#([\w-]+)', html) + re.findall(
        r"explainer\.html#([\w-]+)", index
    )
    assert links, "expected deep links in the explainer and README"
    for link in links:
        if m := re.fullmatch(r"flow-([a-z-]+?)-(\d+)", link):
            assert m.group(1) in flows, link
            assert 1 <= int(m.group(2)) <= flows[m.group(1)], link
        elif m := re.fullmatch(r"story-(\d+)", link):
            assert 1 <= int(m.group(1)) <= len(gen.STORY.steps), link
        elif m := re.fullmatch(r"stage-([a-z]+)", link):
            assert m.group(1) in stages, link
        elif m := re.fullmatch(r"step-(\d+)", link):
            assert 1 <= int(m.group(1)) <= _old_walkthrough_steps(html), link
        else:
            assert f'id="{link}"' in html, f"#{link} has no target"
    # Each player still parses its own deep-link format (the README and other pages
    # link to #flow-<id>-N and #step-N; the story adds #story-N and #stage-<key>).
    for name in ("flow", "story", "stage", "step"):
        assert f"/^#{name}-(" in html, f"no #{name}- deep-link parser in the explainer"
