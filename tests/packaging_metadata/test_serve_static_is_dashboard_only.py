"""The wheel's ``serve/static`` bundle is the local dashboard, not a website.

``src/novafabric/serve/static/`` is packaged into the wheel (the
``src/novafabric/serve/static/**/*`` include in ``pyproject.toml``) and mounted
at ``/`` by ``nova serve --experimental``. Until ADR-0299 (issue
novafabric-private#20, PR B) ``web/scripts/copy-dashboard.mjs`` copied the
**whole** Astro build into it, so every ``pip install novafabric`` carried a
second, drifting copy of the public site — ``/concepts``, ``/install``,
``/why``, ``/spec``, ``/showcase/*``, ``robots.txt`` — beside the one
``https://novafabric.ai`` actually serves.

Invariants, each one a way the bundle could regress:

* the top level holds only dashboard entries (deny by default, so a new
  marketing route cannot slip in under a name nobody thought to ban);
* the dashboard shell and every asset it reaches are present, so trimming the
  bundle cannot break the dashboard (no dangling chunk import, font or CSS);
* ``/`` forwards to the dashboard keeping ``?token=``, and the 404 page offers
  no root-relative link into a page the bundle no longer has;
* explanatory links leave for ``https://novafabric.ai`` as plain external links
  — never required to operate locally — and point at docs the public site
  really renders.

The checks read the bundle through ``importlib.resources``, i.e. through the
importable ``novafabric`` package: the source tree under an editable install,
the installed files under a wheel install.
"""
from __future__ import annotations

import importlib.resources
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STATIC = Path(str(importlib.resources.files("novafabric") / "serve" / "static"))
COMMANDS = REPO / "web" / "src" / "components" / "dashboard" / "commands"
DASHBOARD_SRC = REPO / "web" / "src" / "components" / "dashboard"

# Everything the bundle may hold at its top level. `topology/` is the TV-5 3D
# view, built by its own step (not copy-dashboard.mjs) and a product feature.
ALLOWED_TOP_LEVEL = {"_astro", "dashboard", "favicon.svg", "index.html", "404.html", "topology"}

# Named explicitly as well, so a failure says *which* retired page came back.
MARKETING = ("concepts", "install", "why", "spec", "showcase", "docs",
             "robots.txt", "llms.txt", "sitemap-index.xml", "sitemap-0.xml")

ROOT_RELATIVE_MARKETING_HREF = re.compile(
    r"""href=["']/(?:concepts|install|why|spec|showcase|docs)(?:[/#"'?])"""
)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_bundle_is_present_and_is_what_the_wheel_packages() -> None:
    """Anti-vacuity, and the link between this tree and the wheel."""
    assert (STATIC / "dashboard" / "index.html").is_file(), (
        f"{STATIC} has no dashboard shell — run `npm run build:dashboard` in web/"
    )
    with (REPO / "pyproject.toml").open("rb") as fh:
        include = tomllib.load(fh)["tool"]["hatch"]["build"]["targets"]["wheel"]["include"]
    assert "src/novafabric/serve/static/**/*" in include


def test_only_dashboard_entries_ship() -> None:
    entries = {p.name for p in STATIC.iterdir()}
    unexpected = sorted(entries - ALLOWED_TOP_LEVEL)
    assert not unexpected, (
        f"serve/static ships {unexpected}, which is not part of the dashboard. "
        "Public pages belong to https://novafabric.ai (ADR-0299); fix "
        "web/scripts/copy-dashboard.mjs rather than deleting build output by hand."
    )


@pytest.mark.parametrize("name", MARKETING)
def test_no_marketing_page_ships(name: str) -> None:
    assert not (STATIC / name).exists(), f"serve/static/{name} is a retired marketing page"


def test_every_asset_the_dashboard_shell_names_exists() -> None:
    shell = _text(STATIC / "dashboard" / "index.html")
    refs = set(re.findall(r"/_astro/([^\"'\s)]+)", shell))
    assert refs, "the dashboard shell references no /_astro/ assets — wrong file?"
    missing = sorted(r for r in refs if not (STATIC / "_astro" / r).is_file())
    assert not missing, f"dashboard shell references assets the bundle lacks: {missing}"


def test_no_chunk_imports_or_styles_reference_a_missing_asset() -> None:
    """Trimming `_astro/` to the dashboard closure must leave nothing dangling."""
    astro = STATIC / "_astro"
    present = {p.name for p in astro.iterdir()}
    dangling: list[str] = []
    for f in sorted(astro.iterdir()):
        if f.suffix not in {".js", ".mjs", ".css"}:
            continue
        text = _text(f)
        names = set(re.findall(r"""["'(]\./([A-Za-z0-9_.@~-]+\.(?:m?js|css))""", text))
        names |= set(re.findall(r"""["'(]/?_astro/([A-Za-z0-9_.@~-]+)""", text))
        dangling += [f"{f.name} -> {n}" for n in sorted(names - present)]
    assert not dangling, "assets referenced but not shipped:\n  " + "\n  ".join(dangling)


def test_root_forwards_to_the_dashboard_keeping_the_token() -> None:
    index = _text(STATIC / "index.html")
    assert "location.replace('/dashboard/' + location.search + location.hash)" in index
    assert 'url=/dashboard/' in index  # no-JS fallback


@pytest.mark.parametrize("page", ["index.html", "404.html", "dashboard/index.html"])
def test_no_page_links_root_relative_into_a_retired_route(page: str) -> None:
    found = ROOT_RELATIVE_MARKETING_HREF.findall(_text(STATIC / page))
    assert not found, f"serve/static/{page} links to {found}, which 404s locally"


def test_404_links_out_to_the_public_site_as_a_plain_external_link() -> None:
    page = _text(STATIC / "404.html")
    external = re.findall(r'<a href="(https?://[^"]+)"([^>]*)>', page)
    assert external, "the 404 page should point readers at the public documentation"
    for url, attrs in external:
        assert url.startswith("https://novafabric.ai/"), url
        assert "noopener" in attrs, url


def test_the_command_docs_link_is_external() -> None:
    """`docsPath` is a site path; rendering it raw would link a 404 locally."""
    usages = [
        (p.relative_to(REPO), line.strip())
        for p in DASHBOARD_SRC.rglob("*.tsx")
        for line in _text(p).splitlines()
        if "docsPath" in line and "href" in line
    ]
    assert usages, "no docsPath link found in the dashboard — the guard is inert"
    raw = [u for u in usages if "publicSiteUrl(" not in u[1]]
    assert not raw, f"docsPath rendered without publicSiteUrl(): {raw}"


def _tracked_publicly() -> set[str]:
    out = subprocess.run(
        ["git", "ls-files", "docs"], cwd=REPO, capture_output=True, text=True, check=True
    ).stdout
    return set(out.splitlines())


def test_command_docs_paths_name_a_page_the_public_site_renders() -> None:
    """novafabric.ai renders `docs/<slug>.md` at `/docs/<slug>/`; check the source."""
    paths: set[str] = set()
    for name in ("commandRegistry.ts", "generatedCommands.ts"):
        paths |= set(re.findall(r"""["']?docsPath["']?\s*:\s*["']([^"']+)["']""", _text(COMMANDS / name)))
    assert paths, "no docsPath values parsed — the guard is inert"
    tracked = _tracked_publicly()
    unresolved = []
    for path in sorted(paths):
        m = re.fullmatch(r"/docs/([a-z0-9/_-]+?)/?(?:#[\w-]+)?", path)
        slug = m.group(1) if m else None
        if not slug or not ({f"docs/{slug}.md", f"docs/{slug}/index.md"} & tracked):
            unresolved.append(path)
    assert not unresolved, (
        f"docsPath values with no public docs page behind them: {unresolved}. "
        "Point them at /docs/<slug>/ for a docs/<slug>.md the public repo tracks."
    )
