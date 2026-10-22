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
"""Erasure claims say only what the code does (ADR-0069 §2 / ADR-0314).

``nova pii erase`` destroys a per-subject DEK. For two release lines nothing in
``src/`` ever *encrypted* anything with that DEK: ``DEKStore.get_or_create_dek``
had no production caller, so "crypto-shredding makes the subject's PII
unreadable" destroyed a key that protected nothing, while docs, ``--help`` and
docstrings said the captured PII was gone.

This guard ties every such claim to a fact derived from the code:

* **wired** — some production module calls ``get_or_create_dek`` *and* AEAD
  encryption, and the ``nova capture`` CLI imports that module. Derived from the
  AST of ``src/``; no module or function name of the implementation is hardcoded.
* while **not wired**, every claim paragraph must carry the phase-A label
  ``key lifecycle only``;
* once **wired**, every claim paragraph must name the opt-in that actually
  encrypts (``--pii-subject``), because capsules captured without it still hold
  plaintext PII and erasure destroys only the key for them — and the phase-A
  label, now false, must be gone everywhere.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from _help_assert import strip_ansi
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "novafabric"

#: The phase-A label, required on every claim while capture does not encrypt.
PHASE_A_LABEL = "key lifecycle only"
#: The opt-in that encrypts, required on every claim once capture does.
OPT_IN = "--pii-subject"

#: Paragraphs about the ADR-0069 per-subject erasure mechanism.
_MECHANISM = re.compile(
    r"pii\s+erase|ADR-0069|DEKStore|erase_subject|dek\.db|per-subject|data\s+subjects?|"
    r"Art\.?\s*17|\berasure\b",
    re.I,
)
#: ... that claim an *effect on the data*: unreadable, erased, crypto-shredded PII.
#: Describing the key operation ("crypto-shred dispatches to the DEK destruction")
#: is not a claim; saying the PII is gone is.
_EFFECT = re.compile(
    r"unrecoverable|unreadable|undecryptable|"
    r"shredded\s+data|"
    r"PII\s+(?:payload\s+)?(?:becomes|is\s+(?:now\s+)?(?:erased|gone|destroyed))|"
    r"erase\s+(?:the\s+)?data\s+subjects?|"
    r"PII\s+crypto-?shred|crypto-?shred\w*\s+(?:erasure|of\s+PII)|"
    r"Art\.?\s*17\s+(?:PII\s+)?(?:erasure\s+)?(?:via\s+)?(?:DEK\s+)?crypto-?shred",
    re.I,
)
#: A *different*, real mechanism: the object capsule store's KMS-wrapped
#: per-object DEKs (ADR-0134/0185/0243, ``trust/envelope_encryption.py``).
#: A paragraph about it alone is not an ADR-0069 claim.
_OBJECT_STORE = re.compile(
    r"ADR-0134|ADR-0185|ADR-0243|KMS-wrapped|wrapped DEK|ShreddedBlobError|"
    r"object capsule store|envelope_encryption|BYOK|\bKEK\b|shred\(\)",
    re.I,
)
_ADR0069_SPECIFIC = re.compile(r"pii\s+erase|ADR-0069|DEKStore|erase_subject|dek\.db", re.I)

#: Historical records, not claims about today: release notes describe what a past
#: version shipped; the decisions index lists ADR *titles*.
_EXCLUDED_DOCS: tuple[str, ...] = ("docs/releases/", "docs/decisions.md")
#: Implementation of the object-store mechanism (see ``_OBJECT_STORE``).
_EXCLUDED_SRC: tuple[str, ...] = (
    "src/novafabric/trust/",
    "src/novafabric/object_capsule_store/",
    "src/novafabric/serve/static/",
)

#: ``nova`` help screens that describe erasure.
_HELP_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("--help",),
    ("pii", "--help"),
    ("pii", "erase", "--help"),
    ("pii", "status", "--help"),
    ("erasure", "--help"),
    ("retention", "apply", "--help"),
)


# ---------------------------------------------------------------------------
# Fact from the code: is capture wired to per-subject envelope encryption?
# ---------------------------------------------------------------------------


def _module_name(path: Path, src_root: Path) -> str:
    rel = path.relative_to(src_root.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _calls_attr(tree: ast.AST, names: set[str]) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name in names:
                return True
    return False


def _imported_modules(tree: ast.AST, own: str) -> set[str]:
    out: set[str] = set()
    pkg = own.rsplit(".", 1)[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = pkg.split(".")[: len(pkg.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            out.add(base)
            out.update(f"{base}.{alias.name}" for alias in node.names)
    return out


def envelope_encryption_callers(src_root: Path) -> set[str]:
    """Modules (outside the DEK store itself) that obtain a per-subject DEK with
    ``get_or_create_dek`` *and* encrypt with it — an AEAD ``.encrypt(...)`` call in
    the same module or in a ``src`` module it imports."""
    trees: dict[str, ast.AST] = {}
    for path in sorted(src_root.rglob("*.py")):
        trees[_module_name(path, src_root)] = ast.parse(path.read_text(encoding="utf-8"))
    encrypting = {m for m, t in trees.items() if _calls_attr(t, {"encrypt"})}
    callers: set[str] = set()
    for mod, tree in trees.items():
        defines = any(
            isinstance(n, ast.FunctionDef) and n.name == "get_or_create_dek"
            for n in ast.walk(tree)
        )
        if defines or not _calls_attr(tree, {"get_or_create_dek"}):
            continue
        reach = {mod} | _imported_modules(tree, mod)
        if reach & encrypting:
            callers.add(mod)
    return callers


def capture_is_wired(src_root: Path) -> bool:
    """True iff the ``nova capture`` CLI imports a module that encrypts with a DEK."""
    callers = envelope_encryption_callers(src_root)
    if not callers:
        return False
    capture_cli = src_root / "cli" / "capture.py"
    if not capture_cli.is_file():
        return False
    own = _module_name(capture_cli, src_root)
    imported = _imported_modules(ast.parse(capture_cli.read_text(encoding="utf-8")), own)
    return any(c in imported or any(i.startswith(c + ".") for i in imported) for c in callers)


# ---------------------------------------------------------------------------
# Claim surfaces
# ---------------------------------------------------------------------------


def _paragraphs(text: str) -> list[str]:
    """Blank-line separated blocks; a table row or a list item is its own unit."""
    out: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        if not block.strip():
            continue
        lines = block.strip().splitlines()
        if all(line.lstrip().startswith("|") for line in lines):
            out.extend(lines)
        else:
            out.extend(u for u in re.split(r"\n(?=[-*] |\d+\. )", block) if u.strip())
    return out


def is_claim(paragraph: str) -> bool:
    flat = " ".join(paragraph.split())
    if not (_MECHANISM.search(flat) and _EFFECT.search(flat)):
        return False
    if _OBJECT_STORE.search(flat) and not _ADR0069_SPECIFIC.search(flat):
        return False
    return True


def _doc_files() -> Iterator[Path]:
    for name in ("README.md", "SECURITY.md", "SAFETY.md", "THREAT_MODEL.md", "llms.txt",
                 "ROADMAP.md"):
        path = REPO / name
        if path.is_file():
            yield path
    for path in sorted((REPO / "docs").rglob("*.md")):
        rel = path.relative_to(REPO).as_posix()
        if any(rel.startswith(ex) for ex in _EXCLUDED_DOCS):
            continue
        yield path


def _src_strings() -> Iterator[tuple[str, str]]:
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(REPO).as_posix()
        if any(rel.startswith(ex) for ex in _EXCLUDED_SRC):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                yield f"{rel}:{node.lineno}", node.value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    v.value for v in node.values
                    if isinstance(v, ast.Constant) and isinstance(v.value, str)
                )
                yield f"{rel}:{node.lineno}", text


def _help_texts() -> Iterator[tuple[str, str]]:
    from novafabric.cli.main import app

    runner = CliRunner()
    for argv in _HELP_COMMANDS:
        result = runner.invoke(app, list(argv), env={"COLUMNS": "200"})
        assert result.exit_code == 0, (argv, result.output)
        yield "nova " + " ".join(argv), strip_ansi(result.output)


def claim_surfaces() -> Iterator[tuple[str, str]]:
    for path in _doc_files():
        rel = path.relative_to(REPO).as_posix()
        for para in _paragraphs(path.read_text(encoding="utf-8")):
            yield rel, para
    for where, text in _src_strings():
        for para in _paragraphs(text):
            yield where, para
    for where, text in _help_texts():
        # Rich boxes wrap help text; join the box rows so a sentence is one unit.
        unboxed = re.sub(r"[│╭╮╰╯─]+", " ", text)
        for para in _paragraphs(unboxed):
            yield where, para


def unqualified_claims(required: str) -> list[str]:
    out: list[str] = []
    for where, para in claim_surfaces():
        flat = " ".join(para.split())
        if is_claim(flat) and required.lower() not in flat.lower():
            out.append(f"{where}: {flat[:160]}")
    return out


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


#: The phase-A wording, false once capture encrypts.
_STALE_PHASE_A = re.compile(r"key\s+lifecycle\s+only|does\s+not\s+yet\s+encrypt", re.I)


def stale_phase_a_labels() -> list[str]:
    return [
        f"{where}: {' '.join(para.split())[:160]}"
        for where, para in claim_surfaces()
        if _STALE_PHASE_A.search(" ".join(para.split()))
    ]


def test_every_erasure_claim_matches_what_capture_does() -> None:
    wired = capture_is_wired(SRC)
    required = OPT_IN if wired else PHASE_A_LABEL
    offenders = unqualified_claims(required)
    if wired:
        offenders += [f"stale phase-A label: {s}" for s in stale_phase_a_labels()]
    state = (
        "capture encrypts PII with per-subject DEKs only under --pii-subject"
        if wired
        else "no production code path encrypts with a per-subject DEK"
    )
    assert not offenders, (
        f"{len(offenders)} erasure claim(s) do not carry {required!r} while {state}. "
        "Label each one (docs-honesty rule):\n  " + "\n  ".join(offenders)
    )


# -- the guard's own detectors (red/green on synthetic input) ---------------


def test_claim_detector_flags_the_original_overclaim() -> None:
    assert is_claim(
        "Destroys the AES-256-GCM DEK for a data subject, rendering all PII "
        "encrypted under that DEK permanently unreadable (nova pii erase)."
    )
    assert is_claim("GDPR Art.17 PII crypto-shredding — nova pii erase.")


def test_claim_detector_ignores_the_object_store_mechanism() -> None:
    assert not is_claim(
        "A shredded envelope is permanently unrecoverable; reads raise the named "
        "ShreddedBlobError (ADR-0134, KMS-wrapped DEK)."
    )
    assert not is_claim("The lineage importer counts unrecoverable legacy edges.")


def test_wiring_detector_requires_a_capture_reachable_encrypting_caller(tmp_path: Path) -> None:
    root = tmp_path / "novafabric"
    (root / "cli").mkdir(parents=True)
    (root / "pii").mkdir()
    (root / "pii" / "store.py").write_text(
        "def get_or_create_dek(subject_id):\n    return subject_id\n"
    )
    # A caller that fetches a DEK but never encrypts does not count.
    (root / "pii" / "lifecycle.py").write_text("s.get_or_create_dek('x')\n")
    (root / "cli" / "capture.py").write_text("from novafabric.pii import lifecycle\n")
    assert envelope_encryption_callers(root) == set()
    assert capture_is_wired(root) is False

    # An encrypting caller the capture CLI never imports does not count either.
    (root / "pii" / "seal.py").write_text(
        "k = s.get_or_create_dek('x')\nAESGCM(k).encrypt(n, b'p', b'a')\n"
    )
    assert envelope_encryption_callers(root) == {"novafabric.pii.seal"}
    assert capture_is_wired(root) is False

    # Imported by the capture CLI: wired.
    (root / "cli" / "capture.py").write_text("from novafabric.pii.seal import thing\n")
    assert capture_is_wired(root) is True

    # The AEAD call may live in a helper module the DEK caller imports.
    (root / "pii" / "seal.py").unlink()
    (root / "pii" / "aead.py").write_text("def seal(k):\n    return AESGCM(k).encrypt(n, p, a)\n")
    (root / "pii" / "stage.py").write_text(
        "from novafabric.pii.aead import seal\nseal(s.get_or_create_dek('x'))\n"
    )
    (root / "cli" / "capture.py").write_text("from novafabric.pii.stage import Stage\n")
    assert envelope_encryption_callers(root) == {"novafabric.pii.stage"}
    assert capture_is_wired(root) is True


@pytest.mark.parametrize("required", [PHASE_A_LABEL, OPT_IN])
def test_unqualified_claim_is_reported(required: str) -> None:
    para = "nova pii erase makes the subject's PII permanently unreadable."
    assert is_claim(para) and required.lower() not in para.lower()
