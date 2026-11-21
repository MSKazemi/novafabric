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

"""The AIUC-1 crosswalk cites IDs only, and its IDs-only catalog runs as documented.

AIUC-1 is a proprietary standard with no reuse licence, so the repository may cite its
control identifiers but must not carry its requirement or evidence-item wording. The
primary guard is structural: the shipped catalog's ``title`` and ``description`` fields
must be empty, so there is no field for standard text to land in. The page is held to
short labels of our own, no long quotations, no conformance claims, and the
non-affiliation disclaimers.

A second, weaker tripwire compares every 3- and 4-word window of the page and the
catalog against SHA-256 digests of a few phrases known to be AIUC-1 titles. The digests
are stored instead of the phrases, so the guard does not itself reproduce them.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.compliance.audit.loader import load_profile

_REPO = Path(__file__).resolve().parents[2]
_DOC = _REPO / "docs" / "aiuc-1-evidence-crosswalk.md"
_CATALOG = _REPO / "examples" / "catalogs" / "aiuc-1-ids-only.yaml"

#: IDs the crosswalk must cover (issue #68).
REQUIRED_ROWS: frozenset[str] = frozenset({
    "E015.1", "E015.2", "E015.3", "E015.4", "D003.3", "D003.4",
    "C002.1", "C002.2", "C002.4", "A006.1", "B001",
})

#: IDs a capsule check can test, in the catalog's order.
CATALOG_IDS: list[str] = ["E015.1", "E015.2", "E015.4", "D003.3", "A006.1"]

PROJECT_DISCLAIMER = (
    "NovaFabric is not affiliated with, endorsed by, or certified by the publishers of these "
    "frameworks. A mapping shows which recorded evidence exists; it does not certify that a "
    "control is met."
)

#: SHA-256 of lower-cased, punctuation-stripped title phrases of AIUC-1 requirements.
TITLE_PHRASE_DIGESTS: frozenset[str] = frozenset({
    "cc28b4b576dd6dd37a223d6666b46d7fe8bbda624ec8b2f70924e767abf4a775",
    "6cf67c21c9a6572e0539a0591fd1af9732f558211278536b05720ead309d6321",
    "b9dac59907a5d9eafb3fd537260a97314108343036241932e86871aca3043dde",
    "40f96e3f8bf8ea32f971b037bb85d4f86b947407a92add8d8640fcdfbb966f2b",
})

_ID = re.compile(r"^[A-F]\d{3}(?:\.\d+)?$")
_NOT_AFFILIATED = re.compile(r"not affiliated with, endorsed by, or certified by [^.]*\.")
_CLAIM = re.compile(
    r"\b(?:compliant|compliance with aiuc\S*|satisf(?:y|ies|ied)|required by aiuc\S*|"
    r"certified|aiuc-1[- ]approved)\b",
    re.I,
)
_QUOTED = re.compile(r"[\"“]([^\"”\n]+)[\"”]")


def _flat(path: Path) -> str:
    """The file as one line, with blockquote and comment markers removed."""
    lines = path.read_text(encoding="utf-8").splitlines()
    return " ".join(" ".join(re.sub(r"^\s*[>#]+\s?", "", line) for line in lines).split())


def _windows(text: str, sizes: tuple[int, ...] = (3, 4)) -> set[str]:
    words = re.sub(r"[^a-z0-9]+", " ", text.lower()).split()
    return {" ".join(words[i:i + n]) for n in sizes for i in range(len(words) - n + 1)}


def _digest_hits(text: str, digests: frozenset[str]) -> set[str]:
    return {w for w in _windows(text) if hashlib.sha256(w.encode()).hexdigest() in digests}


def _crosswalk_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in _DOC.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| \*\*([A-F]\d{3}(?:\.\d+)?)\*\* \|(.*)\|$", line)
        if m:
            rows[m.group(1)] = [cell.strip() for cell in m.group(2).split(" | ")]
    return rows


# ---------------------------------------------------------------- the catalog


def test_the_catalog_loads_with_the_real_loader() -> None:
    profile = load_profile(str(_CATALOG))
    assert profile.framework == "AIUC-1"
    assert [c.id for c in profile.controls] == CATALOG_IDS


def test_the_catalog_holds_ids_only() -> None:
    raw = yaml.safe_load(_CATALOG.read_text(encoding="utf-8"))
    for control in raw["controls"]:
        assert _ID.match(control["id"]), control["id"]
        assert control["title"] == "", control["id"]
        assert control["description"] == "", control["id"]


def test_the_catalog_evidence_descriptions_name_novafabric_checks() -> None:
    raw = yaml.safe_load(_CATALOG.read_text(encoding="utf-8"))
    for control in raw["controls"]:
        for req in control["evidence_requirements"]:
            text = req["description"]
            assert len(text) <= 80, (control["id"], text)
            assert re.search(r"\.jsonl?\b|NovaSeal|capsule", text), (control["id"], text)


def test_the_catalog_gives_the_result_the_page_prints(tmp_path: Path) -> None:
    cap = tmp_path / "data" / "capsules" / "demo-run"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: demo-run\n", encoding="utf-8")
    (cap / "model-calls.jsonl").write_text('{"model": "m"}\n', encoding="utf-8")
    (cap / "tool-calls.jsonl").write_text('{"tool": "search"}\n', encoding="utf-8")
    result = CliRunner().invoke(
        app,
        ["audit", "map", "--profile", str(_CATALOG), "--data-dir", str(tmp_path / "data"),
         "--capsule", "demo-run", "--json"],
    )
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1  # the page says: a missing required control exits 1
    status = {c["control_id"]: c["status"] for c in json.loads(result.output)["coverages"]}
    assert status == {
        "E015.1": "covered", "E015.2": "covered", "D003.3": "covered",
        "E015.4": "missing", "A006.1": "missing",
    }


def test_every_catalog_id_is_a_row_of_the_crosswalk() -> None:
    assert set(CATALOG_IDS) <= set(_crosswalk_rows())


# ---------------------------------------------------------------- the page


def test_the_crosswalk_covers_the_required_ids() -> None:
    assert REQUIRED_ROWS <= set(_crosswalk_rows())


def test_every_row_rates_its_strength_and_names_a_gap() -> None:
    for cid, cells in _crosswalk_rows().items():
        assert len(cells) == 5, cid
        area, _status, record, strength, gap = cells
        assert len(area) <= 50, (cid, area)  # a short label of ours, not the standard's title
        assert strength.split(":")[0] in {"Strong", "Partial"}, (cid, strength)
        assert record and gap, cid


def test_the_page_carries_both_disclaimers() -> None:
    text = _flat(_DOC)
    assert PROJECT_DISCLAIMER in text
    assert "NovaFabric is not affiliated with, endorsed by, or certified by AIUC" in text
    assert "licensed copy" in text


def test_the_c002_4_and_a006_1_caveats_stay() -> None:
    rows = _crosswalk_rows()
    assert "open-weight" in rows["C002.4"][4]
    assert "full prompts and responses" in rows["A006.1"][4]
    assert "risk" in rows["A006.1"][3]


def test_no_conformance_claims() -> None:
    for path in (_DOC, _CATALOG):
        text = _NOT_AFFILIATED.sub("", _flat(path))
        assert _CLAIM.findall(text) == [], path.name


def test_no_long_quotations() -> None:
    # Standard text would arrive as a quotation; our own prose needs none of five words.
    for path in (_DOC, _CATALOG):
        long_quotes = [q for q in _QUOTED.findall(path.read_text(encoding="utf-8"))
                       if len(q.split()) >= 5]
        assert long_quotes == [], path.name


def test_no_known_title_phrase_appears() -> None:
    for path in (_DOC, _CATALOG):
        assert _digest_hits(path.read_text(encoding="utf-8"), TITLE_PHRASE_DIGESTS) == set()


def test_the_title_tripwire_catches_an_injected_phrase() -> None:
    planted = frozenset({hashlib.sha256(b"alpha beta gamma").hexdigest()})
    assert _digest_hits("Intro. Alpha, beta -- Gamma! outro", planted) == {"alpha beta gamma"}
    assert _digest_hits("alpha gamma beta", planted) == set()


def test_the_page_is_listed_in_the_docs_index() -> None:
    index = (_REPO / "docs" / "README.md").read_text(encoding="utf-8")
    assert "aiuc-1-evidence-crosswalk.md" in index
