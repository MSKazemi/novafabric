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

Two weaker tripwires compare the page and the catalog against SHA-256 digests of the
standard's text, stored instead of the text so the guard does not itself reproduce it:
every window of 3 to 4 words against a few known titles, and every window against the
digest file ``aiuc1_standard_digests.txt``: all control and evidence-item titles of three
or more words, and every 8-word run of the controls and evidence items the page cites.
An unquoted run of eight words or more copied from the standard fails the second one.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from typer.testing import CliRunner

from novafabric.capture.finalize import evidence_digests
from novafabric.cli.main import app
from novafabric.compliance.audit.loader import load_profile
from novafabric.trust.novaseal import KeyConfig, NovaSeal

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

_DIGESTS = Path(__file__).with_name("aiuc1_standard_digests.txt")

#: The one sentence that mentions certification of AIUC-1 outside the disclaimer. It says
#: who publishes the standard and that this page makes no claim; nothing about how AIUC
#: or an auditor assesses anything, which this project cannot vouch for.
CERTIFICATION_SENTENCE = (
    "AIUC-1 is published by AIUC; this page does not claim or imply certification "
    "against it, and only your auditor decides whether a record meets a requirement."
)

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


def _norm_words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()


def _standard_digest_hits(text: str, digests: Path = _DIGESTS) -> dict[str, set[str]]:
    """Windows of *text* whose digest is one of the stored title or 8-word digests."""
    titles: dict[int, set[str]] = {}
    grams: set[str] = set()
    for line in digests.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        kind, digest = line.split(" ")
        if kind == "G8":
            grams.add(digest)
        else:
            titles.setdefault(int(kind[1:]), set()).add(digest)
    words = _norm_words(text)
    hits: dict[str, set[str]] = {"title": set(), "run": set()}
    for n, wanted in titles.items():
        for i in range(len(words) - n + 1):
            window = " ".join(words[i:i + n])
            if hashlib.sha256(window.encode()).hexdigest() in wanted:
                hits["title"].add(window)
    for i in range(len(words) - 7):
        window = " ".join(words[i:i + 8])
        if hashlib.sha256(window.encode()).hexdigest()[:16] in grams:
            hits["run"].add(window)
    return hits


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


# ------------------------------------------- review findings on the crosswalk (#68)


def test_the_digest_file_holds_digests_only() -> None:
    lines = [ln for ln in _DIGESTS.read_text(encoding="utf-8").splitlines()
             if ln and not ln.startswith("#")]
    assert len(lines) > 1500
    for line in lines:
        assert re.fullmatch(r"T\d+ [0-9a-f]{64}|G8 [0-9a-f]{16}", line), line[:40]


def test_no_unquoted_eight_word_run_is_copied_from_the_standard() -> None:
    for path in (_DOC, _CATALOG):
        hits = _standard_digest_hits(path.read_text(encoding="utf-8"))
        assert len(hits["run"]) == 0, (path.name, len(hits["run"]))


def test_no_control_or_evidence_title_appears_verbatim() -> None:
    for path in (_DOC, _CATALOG):
        hits = _standard_digest_hits(path.read_text(encoding="utf-8"))
        assert len(hits["title"]) == 0, (path.name, len(hits["title"]))


def test_the_guards_have_teeth(tmp_path: Path) -> None:
    run = "alpha beta gamma delta epsilon zeta eta theta"
    title = "log of something plausible"
    planted = tmp_path / "digests.txt"
    planted.write_text(
        f"G8 {hashlib.sha256(run.encode()).hexdigest()[:16]}\n"
        f"T4 {hashlib.sha256(title.encode()).hexdigest()}\n",
        encoding="utf-8",
    )
    hits = _standard_digest_hits(f"Intro. {run.title()}, then {title.upper()}!", planted)
    assert hits == {"title": {title}, "run": {run}}
    # Seven words, or the same words reordered, are not the planted run.
    assert _standard_digest_hits("alpha beta gamma delta epsilon zeta eta", planted)["run"] == set()
    assert _standard_digest_hits("beta alpha gamma delta epsilon zeta eta theta", planted)[
        "run"
    ] == set()


def test_the_certification_sentence_is_pinned() -> None:
    text = _flat(_DOC)
    assert CERTIFICATION_SENTENCE in text
    # Nothing about how AIUC or an auditor assesses: this project cannot vouch for it.
    assert "accredited" not in text.lower()
    assert "evaluations" not in text.lower()
    assert "takes part in neither" not in text


def test_validate_takes_a_capsule_directory_not_a_run_id() -> None:
    text = _DOC.read_text(encoding="utf-8")
    assert "nova validate <run-id>" not in text
    assert "`nova validate <capsule-dir>`" in _crosswalk_rows()["E015.1"][2]


def test_e015_2_is_rated_for_what_its_check_shows() -> None:
    rows = _crosswalk_rows()
    _area, _status, record, strength, gap = rows["E015.2"]
    assert strength.split(":")[0] == "Partial"
    requirements = {
        c.id: [r.evidence_type for r in c.evidence_requirements]
        for c in load_profile(str(_CATALOG)).controls
    }
    # The page and the catalog agree: the check is a recorded model call, as for E015.1.
    assert requirements["E015.2"] == ["model_calls"]
    assert "same check as E015.1" in gap
    assert "does not check `trace.jsonl`" in gap
    # The record cell must not present trace spans as what the catalog checks.
    assert "catalog" in record and "model call" in record
    for claim in ("execution chain", "delegation chain", "full agent"):
        assert claim not in (record + gap).lower()
    raw = yaml.safe_load(_CATALOG.read_text(encoding="utf-8"))
    e015_2 = next(c for c in raw["controls"] if c["id"] == "E015.2")
    for req in e015_2["evidence_requirements"]:
        assert "trace" not in req["description"].lower()
        assert "span" not in req["description"].lower()


def test_e015_4_is_partial_and_states_the_legacy_seal_limit() -> None:
    _area, _status, record, strength, gap = _crosswalk_rows()["E015.4"]
    assert strength.split(":")[0] == "Partial"
    # A seal made before evidence digests verifies but does not bind the logs (#318).
    assert "before evidence digests" in gap
    assert "does not bind" in gap
    assert "`nova verify <capsule>`" in gap
    assert "NOT PRESENT" in gap
    assert "digest-bearing seal" in gap or "evidence digests" in gap


def test_e015_4_catalog_description_warns_that_a_seal_may_not_bind_the_logs() -> None:
    raw = yaml.safe_load(_CATALOG.read_text(encoding="utf-8"))
    e015_4 = next(c for c in raw["controls"] if c["id"] == "E015.4")
    (req,) = e015_4["evidence_requirements"]
    assert req["evidence_type"] == "seal_signature"
    assert "may not bind" in req["description"]


def test_a006_1_says_a_redaction_manifest_exists_not_that_data_is_filtered() -> None:
    _area, _status, record, _strength, gap = _crosswalk_rows()["A006.1"]
    assert "redaction manifest exists" in gap
    assert "not that" in gap and "filtered" in gap
    raw = yaml.safe_load(_CATALOG.read_text(encoding="utf-8"))
    a006 = next(c for c in raw["controls"] if c["id"] == "A006.1")
    (req,) = a006["evidence_requirements"]
    assert req["evidence_type"] == "pii_scan"
    assert "not proof" in req["description"]


# --- E015.4 is covered only for a sealed capsule (the catalog's evidence type) -------


def _seal_capsule(root: Path, name: str, *, bind_evidence: bool) -> Path:
    """A capsule under *root* with a genuine NovaSeal ``.seal/`` (local key, no TSA).

    ``bind_evidence=False`` seals a manifest without ``evidence_digests``: the shape of a
    seal made before evidence digests, which signs the manifest only."""
    work = root.parent / f"_seal-{name}"
    work.mkdir(parents=True)
    cap = root / name
    cap.mkdir(parents=True)
    (cap / "model-calls.jsonl").write_text('{"model": "m"}\n', encoding="utf-8")
    (cap / "tool-calls.jsonl").write_text('{"tool": "t"}\n', encoding="utf-8")
    manifest: dict[str, Any] = {
        "run_id": name, "status": "success",
        "created_at": "2024-01-01T00:00:00.000000Z",
        "finished_at": "2024-01-01T00:00:10.000000Z",
    }
    if bind_evidence:
        manifest["evidence_digests"] = evidence_digests(cap)
    (cap / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "aiuc1-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256())
    )
    key_path, cert_path = work / "seal.key", work / "seal.crt"
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    bundle = NovaSeal(
        config=KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path)),
        tsa_url="", db_path=str(work / "merkle.db"),
    ).seal(manifest)
    seal_dir = cap / ".seal"
    seal_dir.mkdir()
    (seal_dir / "manifest.dsse").write_bytes(bundle.dsse_envelope)
    (seal_dir / "manifest.dsse.tsr").write_bytes(bundle.tsr)
    (seal_dir / "log-entry.json").write_text(json.dumps(bundle.log_entry), encoding="utf-8")
    return cap


def _audit_status(data_dir: Path, capsule: str) -> dict[str, str]:
    result = CliRunner().invoke(
        app,
        ["audit", "map", "--profile", str(_CATALOG), "--data-dir", str(data_dir),
         "--capsule", capsule, "--json"],
    )
    assert isinstance(result.exception, SystemExit), result.exception
    return {c["control_id"]: c["status"] for c in json.loads(result.output)["coverages"]}


@pytest.mark.parametrize("bind_evidence", [True, False], ids=["digest-bearing", "legacy"])
def test_e015_4_is_covered_for_a_sealed_capsule(
    tmp_path: Path, bind_evidence: bool
) -> None:
    capsules = tmp_path / "data" / "capsules"
    _seal_capsule(capsules, "sealed-run", bind_evidence=bind_evidence)
    status = _audit_status(tmp_path / "data", "sealed-run")
    # The check passes for both, which is why the page rates E015.4 Partial and says a
    # seal made before evidence digests does not bind the logs.
    assert status["E015.4"] == "covered"


def test_e015_4_is_missing_for_an_unsealed_capsule(tmp_path: Path) -> None:
    cap = tmp_path / "data" / "capsules" / "plain-run"
    cap.mkdir(parents=True)
    (cap / "capsule.yaml").write_text("run_id: plain-run\n", encoding="utf-8")
    (cap / "model-calls.jsonl").write_text('{"model": "m"}\n', encoding="utf-8")
    (cap / "tool-calls.jsonl").write_text('{"tool": "t"}\n', encoding="utf-8")
    status = _audit_status(tmp_path / "data", "plain-run")
    assert status["E015.4"] == "missing"
    assert status["E015.1"] == status["E015.2"] == status["D003.3"] == "covered"
