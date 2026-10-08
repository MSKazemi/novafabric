"""The public claim surfaces say only what the code does (brand claim matrix, #17).

The README, ``llms.txt``, the press kit, ``CITATION.cff``, ``.zenodo.json``, the GitHub
Action and the Claude Code plugin are copied verbatim by search engines, answer engines,
journalists and package indexes. On 2026-10-09 they still carried claims the code had
outgrown or never met, each one copied from a file that had been fixed elsewhere:

* "secret-redacted Run Capsules" in ``CITATION.cff`` and ``.zenodo.json``, and
  "proof no secrets leaked" next to ``redaction-proof.json``. The scanner matches
  known key formats; it cannot prove no secret remains.
* "four replay modes" in ``CITATION.cff``, ``.zenodo.json`` and the README comparison
  table, while ``ReplayFlags.mode`` has had five since ``intervention`` landed.
* "signed Run Capsules" in the plugin. Capture seals only once a signing profile is
  configured (``capture/orchestrator.py::_seal_capsule``; ADR-0301 keeps it opt-in).
* "14 API-key and token rules" in the README after the rule pack grew to 18.

Each check below reads the number or the property from the code, so the next change
to the code fails here instead of drifting in public.
"""

from __future__ import annotations

import re
import tomllib
import typing
from pathlib import Path

import pytest
import yaml

from novafabric.capture import secrets
from novafabric.replay._flags import ReplayFlags

REPO = Path(__file__).resolve().parents[2]

#: Files whose wording is copied verbatim by third parties.
CLAIM_SURFACES: tuple[str, ...] = (
    "README.md",
    "llms.txt",
    "CITATION.cff",
    ".zenodo.json",
    "pyproject.toml",
    "docs/press-kit.md",
    "docs/faq.md",
    "docs/getting-started.md",
    "docs/concepts.md",
    "docs/comparison.md",
    "examples/capsules/README.md",
    ".github/actions/capture/action.yml",
    "integrations/claude-plugin/README.md",
    "integrations/claude-plugin/.claude-plugin/plugin.json",
    "integrations/claude-plugin/skills/novafabric-instrument/SKILL.md",
    "integrations/claude-plugin/skills/novafabric-deploy/SKILL.md",
)

#: Claims the code does not support. A phrase inside double quotes is a quotation
#: (the press kit lists the wording *not* to use), so a leading quote exempts it.
BANNED: dict[str, re.Pattern[str]] = {
    "secret-redacted as a capsule property": re.compile(r'(?<!")\bsecret-redacted\b', re.I),
    "proof that no secret remains": re.compile(
        r"(?<!not\s)(?:proof|proves?)\s+(?:that\s+)?no\s+secrets?\s+(?:leaked|appear|remain)",
        re.I,
    ),
    "a stale replay-mode count": re.compile(
        r"\b(?:four|4)\s+(?:explicit\s+)?(?:replay\s+)?modes\b(?!\s+of)", re.I
    ),
    "default signing of capsules": re.compile(
        r'(?<!")\bsigned,?\s+(?:replayable,?\s+|verifiable,?\s+)*(?:Run\s+)?Capsules?\b', re.I
    ),
    "unscoped flight-simulator replay": re.compile(r"flight\s+simulator|re-fl(?:y|ies)\s+the\s+route", re.I),
}


def _lines(rel: str) -> list[str]:
    return (REPO / rel).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("label", sorted(BANNED))
def test_no_claim_surface_makes_an_unsupported_claim(label: str) -> None:
    pattern = BANNED[label]
    offenders = [
        f"{rel}:{number}: {line.strip()[:120]}"
        for rel in CLAIM_SURFACES
        for number, line in enumerate(_lines(rel), 1)
        if pattern.search(line)
    ]
    assert not offenders, f"{label}:\n" + "\n".join(offenders)


def test_the_banned_patterns_match_the_phrasings_they_exist_to_catch() -> None:
    """Non-vacuity: each pattern catches the wording that actually shipped."""
    shipped = {
        "secret-redacted as a capsule property": "as portable, secret-redacted Run Capsules",
        "proof that no secret remains": "redaction-proof.json  <- proof no secrets leaked",
        "a stale replay-mode count": "and four replay modes (forensic, mocked, semantic, exact)",
        "default signing of capsules": "record runs as signed, replayable Run Capsules",
        "unscoped flight-simulator replay": "NovaFabric is a flight simulator - it re-flies the route.",
    }
    assert set(shipped) == set(BANNED)
    for label, phrase in shipped.items():
        assert BANNED[label].search(phrase), f"{label!r} no longer matches {phrase!r}"


def test_the_banned_patterns_leave_true_statements_alone() -> None:
    for legitimate in (
        'write "secret-scanned", not "secret-redacted"',
        "a signed Evidence Bundle that verifies offline",
        "the four single-capsule modes of",
        "It is not proof that no secret remains.",
        "Run Capsules can be sealed with the user's own key",
    ):
        for label, pattern in BANNED.items():
            assert not pattern.search(legitimate), f"{label!r} over-matched {legitimate!r}"


def test_readme_secret_rule_count_matches_the_rule_pack() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    counts = re.findall(r"(\d+) API-key and token rules", readme)
    assert counts, "README no longer states the secret-scanner rule count"
    assert {int(c) for c in counts} == {len(secrets._RULES)}, (
        f"README says {counts} rules; {secrets.PACK_NAME} {secrets.PACK_VERSION} "
        f"has {len(secrets._RULES)}"
    )


_NUMBER_WORDS = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}


def _replay_modes() -> tuple[str, ...]:
    return typing.get_args(typing.get_type_hints(ReplayFlags)["mode"])


def test_readme_replay_mode_count_and_table_match_the_engine() -> None:
    modes = _replay_modes()
    word = _NUMBER_WORDS[len(modes)]
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert f"**{word} explicit, falsifiable modes**" in readme
    for mode in modes:
        assert re.search(rf"^\| `{mode}`", readme, re.M), f"README replay table lacks `{mode}`"
    llms = (REPO / "llms.txt").read_text(encoding="utf-8")
    assert f"**Replay modes:** {word}" in llms


def test_citation_and_zenodo_name_every_replay_mode() -> None:
    modes = _replay_modes()
    word = _NUMBER_WORDS[len(modes)]
    for rel in ("CITATION.cff", ".zenodo.json"):
        text = " ".join((REPO / rel).read_text(encoding="utf-8").split())
        assert f"{word} " in text and "replay modes" in text, f"{rel} lost its mode count"
        for mode in modes:
            assert mode in text, f"{rel} does not name replay mode {mode!r}"


def test_pypi_keywords_mirror_citation_cff() -> None:
    """pyproject.toml says its keywords mirror CITATION.cff; hold it to that."""
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    citation = yaml.safe_load((REPO / "CITATION.cff").read_text(encoding="utf-8"))
    extra = set(project["keywords"]) - set(citation["keywords"])
    assert not extra, f"PyPI keywords not in CITATION.cff: {sorted(extra)}"
