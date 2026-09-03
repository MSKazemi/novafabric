#!/usr/bin/env python3
"""Dependency license-policy gate (ADR-0024).

Closes ADR-0024's own stated gap — *"The CI enforcement step is not yet
implemented; manual review is the gap."* The tier policy has been written down
since 2026-05; until now nothing checked it, and the per-dependency tier
reasoning lived only in human-readable ``pyproject.toml`` comments.

Walks every installed distribution, resolves its license, maps it to an
ADR-0024 tier, and decides what blocks CI:

- **Tier A** (Apache-2.0, MIT, BSD, PostgreSQL, PSF, ISC, CC0/Public Domain,
  Unlicense) — passes silently. No paperwork for permissive licenses.
- **Tier B** (LGPL-2.1/3.0 dynamic-linking-only, MPL-2.0, Apache-2.0 with
  patent-retaliation amendments) — allowed **only** when declared in
  ``.license-policy.toml`` with a justification. An undeclared Tier B blocks.
- **Tier C** (GPL any linking, AGPL, SSPL, BSL, Elastic, Confluent Community,
  Commons Clause, source-available, proprietary) — *"reject by default;
  explicit waiver required."* Blocks unless declared **with a migration path**,
  because ADR-0024 admits one only with "the business justification and the
  migration path away from the dependency".
- **Tier D** (field-of-use restrictions, "ethical source" terms) — **forbidden
  outright.** ``"D"`` is not a declarable tier, so no entry can reach it.
- **Unknown** — a license we cannot resolve blocks until declared. Fail closed,
  the same posture as the pip-audit gate's UNKNOWN severity.

Two resolution subtleties this gate gets right, both found by measuring the
real tree rather than assuming:

1. **Disjunctive licenses resolve to their most permissive tier.** ``pyphen``
   is tri-licensed GPLv2+ / LGPLv2+ / MPL-1.1; the consumer chooses, so it is
   Tier B via LGPL, not Tier C via GPL. A naive ``grep GPL`` — which is what
   the Go collector's gate does — false-positives it. Conjunctive (``AND``)
   licenses resolve to their *least* permissive tier instead.
2. **Modern wheels may declare a license only as a file.** ``sigstore-models``
   and ``google-crc32c`` carry no classifier and no ``License`` field, only
   PEP 639 ``License-File``; resolution falls back to sniffing that file's
   text.

Anything not named by ADR-0024 is deliberately left ``unknown`` rather than
guessed into a tier — this gate enforces the policy, it does not extend it.

Stdlib only: a supply-chain gate that grows the supply chain to do its job
defeats its own purpose. Exit codes: 0 clean, 1 policy violation, 2 malformed
policy file.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from importlib.metadata import Distribution, distributions
from pathlib import Path

REQUIRED_DECLARATION_KEYS = frozenset({"package", "license", "tier", "justification"})
# Tier C additionally requires a migration path: ADR-0024 §Tier C says adoption
# "requires an ADR stating the business justification and the migration path
# away from the dependency."
OPTIONAL_DECLARATION_KEYS = frozenset({"migration_path"})
# Tier D is absent by design — ADR-0024 §Tier D is "Forbidden", with no waiver.
VALID_DECLARED_TIERS = frozenset({"A", "B", "C", "unknown"})

# Ordered rules — first match wins, so the specific pattern must precede the
# general one. LGPL is tested before GPL because "GNU Lesser General Public
# License" contains "General Public License"; AGPL before both for the same
# reason. Patterns are matched against a normalized (lower-cased) license
# string drawn from PEP 639 expressions, trove classifiers, or license text.
_TIER_RULES: tuple[tuple[str, str], ...] = (
    # --- Tier D: forbidden outright (ADR-0024 §Tier D) --------------------
    # Field-of-use restrictions and "ethical source" terms. These break OSI
    # compliance and create distribution uncertainty, so unlike Tier C there
    # is no waiver: the dependency cannot be adopted at all.
    (r"good, not evil", "D"),
    (r"hippocratic|do no harm licen[cs]e", "D"),
    (r"anti-?996", "D"),
    (r"no military use|non-?military|no fossil", "D"),
    (r"ethical source", "D"),
    # --- Tier C: reject by default, ADR-waivable (ADR-0024 §Tier C) -------
    (r"\bagpl|affero", "C"),
    (r"\bsspl\b|server side public license", "C"),
    (r"business source license|\bbsl-1\.1\b", "C"),
    (r"elastic license", "C"),
    (r"confluent community", "C"),
    (r"commons clause", "C"),
    (r"functional source license|source-available", "C"),
    (r"\bproprietary\b", "C"),
    # --- Tier B: allowed with a recorded justification (§Tier B) ---------
    (r"\blgpl|lesser general public", "B"),
    (r"\bmpl-2\.0\b|mozilla public license 2\.0", "B"),
    # --- Tier C: GPL, after LGPL/AGPL have had their chance ---------------
    (r"\bgpl|general public license", "C"),
    # --- Tier A: permissive (§Tier A) ------------------------------------
    (r"\bapache\b", "A"),
    (r"\bmit\b", "A"),
    (r"\bbsd\b", "A"),
    (r"postgresql license", "A"),
    (r"python software foundation|\bpsf\b", "A"),
    (r"\bisc\b", "A"),
    (r"public domain|\bcc0\b", "A"),
    (r"\bunlicense\b", "A"),
    (r"zope public license", "A"),
    # 0BSD / Zlib / CNRI-Python are permissive and PSF/BSD-family. They are not
    # named in ADR-0024's Tier A list only because that list predates them
    # appearing in the tree; see the 2026-08-02 amendment.
    (r"\b0bsd\b", "A"),
    (r"\bzlib\b", "A"),
    (r"\bcnri\b", "A"),
)

# Least-permissive-first, used to combine multi-license expressions.
_TIER_ORDER = {"A": 0, "B": 1, "unknown": 2, "C": 3, "D": 4}


class PolicyError(Exception):
    """Malformed .license-policy.toml — fails the gate, never silently skipped."""


@dataclass(frozen=True)
class Declaration:
    package: str
    license_id: str
    tier: str
    justification: str
    migration_path: str = ""


@dataclass(frozen=True)
class Resolved:
    package: str
    version: str
    license_text: str
    tier: str
    source: str

    @property
    def label(self) -> str:
        return (
            f"{self.package} {self.version} — {self.license_text} [{self.tier}] (via {self.source})"
        )


def classify(license_string: str, *, prose: bool = False) -> str:
    """Map one license string to an ADR-0024 tier.

    ``prose=True`` marks free license *text* rather than an identifier. That
    distinction is load-bearing: Apache-2.0's own body contains "USE,
    REPRODUCTION, AND DISTRIBUTION" and MIT's contains "and", so parsing
    license text as an SPDX expression splits it on those words and resolves
    the fragments to ``unknown``. Prose therefore takes the first matching
    rule instead — the rule table is ordered deny-first, so quoting another
    license later in the body cannot downgrade the verdict.

    For identifiers, ``AND`` takes the *least* permissive tier (every term
    binds) and ``OR`` the *most* permissive (the consumer chooses). Only the
    SPDX operators split; ``/`` deliberately does not, because trove
    classifiers use it inside single names such as "Other/Proprietary
    License". Returns ``"unknown"`` when nothing matches — never a guess.
    """
    text = license_string.strip().lower()
    if not text:
        return "unknown"

    if not prose:
        parts = [p for p in re.split(r"\band\b", text) if p.strip()]
        if len(parts) > 1:
            return max((classify(p) for p in parts), key=lambda t: _TIER_ORDER[t])

        parts = [p for p in re.split(r"\bor\b", text) if p.strip()]
        if len(parts) > 1:
            return min((classify(p) for p in parts), key=lambda t: _TIER_ORDER[t])

    for pattern, tier in _TIER_RULES:
        if re.search(pattern, text):
            return tier
    return "unknown"


def resolve_license(dist: Distribution) -> tuple[str, str]:
    """Return ``(license_string, source)`` for a distribution.

    Resolution order follows how reliably each source states a license:
    PEP 639 ``License-Expression`` (an SPDX expression, authoritative) →
    trove classifiers → the legacy free-text ``License`` field → the text of
    the declared ``License-File``. Returns ``("", "none")`` when every source
    is silent, which the caller treats as ``unknown`` and blocks on.
    """
    md = dist.metadata

    expression = md.get("License-Expression")
    if expression and expression.strip():
        return expression.strip(), "License-Expression"

    classifiers = [
        c.split("::")[-1].strip()
        for c in (md.get_all("Classifier") or [])
        if str(c).startswith("License ::")
    ]
    if classifiers:
        return " OR ".join(classifiers), "classifier"

    legacy = (md.get("License") or "").strip()
    # Some projects paste an entire license text into this field; only treat it
    # as an identifier when it is short enough to actually be one.
    if legacy and len(legacy) <= 200:
        return legacy, "License field"

    for name in md.get_all("License-File") or []:
        text = _read_license_file(dist, str(name))
        if text:
            return text, f"License-File:{name}"

    return "", "none"


def _read_license_file(dist: Distribution, name: str) -> str:
    """Return a short signature of a distribution's license file, or ``""``.

    Only the head of the file is used: enough to identify the license, cheap to
    read, and immune to the body text of one license quoting another.
    """
    for candidate in dist.files or []:
        if str(candidate).endswith(name):
            try:
                content = dist.read_text(str(candidate))
            except (OSError, UnicodeDecodeError):
                content = None
            if not content:
                located = Path(str(dist.locate_file(candidate)))
                try:
                    content = located.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
            return " ".join(content.split()[:40])
    return ""


def load_declarations(path: Path) -> dict[str, Declaration]:
    """Parse ``.license-policy.toml``. A malformed file fails the gate."""
    if not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyError(f"{path}: unreadable or malformed TOML: {exc}") from exc

    out: dict[str, Declaration] = {}
    for entry in raw.get("declaration", []):
        if not isinstance(entry, dict):
            raise PolicyError(f"{path}: every [[declaration]] must be a table")
        keys = set(entry)
        missing = sorted(REQUIRED_DECLARATION_KEYS - keys)
        extra = sorted(keys - REQUIRED_DECLARATION_KEYS - OPTIONAL_DECLARATION_KEYS)
        if missing or extra:
            raise PolicyError(
                f"{path}: declaration {entry.get('package', '?')!r} has "
                f"missing={missing} unexpected={extra}; required "
                f"{sorted(REQUIRED_DECLARATION_KEYS)}, optional "
                f"{sorted(OPTIONAL_DECLARATION_KEYS)}"
            )
        tier = str(entry["tier"])
        if tier not in VALID_DECLARED_TIERS:
            raise PolicyError(
                f"{path}: declaration {entry['package']!r} has tier {tier!r}; "
                f"only {sorted(VALID_DECLARED_TIERS)} may be declared — ADR-0024 "
                "§Tier D is forbidden outright and has no waiver path"
            )
        # A declaration must agree with its own license string. This is what
        # stops a declaration from being used to launder a license into a
        # friendlier tier: the tier is re-derived from the text, not trusted.
        derived = classify(str(entry["license"]))
        if derived != tier:
            raise PolicyError(
                f"{path}: declaration {entry['package']!r} claims tier {tier!r} but its "
                f"license {str(entry['license'])!r} classifies as {derived!r}"
            )
        justification = str(entry["justification"]).strip()
        if len(justification) < 20:
            raise PolicyError(
                f"{path}: declaration {entry['package']!r} needs a real "
                "justification (>= 20 chars), not a placeholder"
            )
        migration_path = str(entry.get("migration_path", "")).strip()
        if tier == "C" and len(migration_path) < 20:
            raise PolicyError(
                f"{path}: declaration {entry['package']!r} is Tier C, which ADR-0024 "
                "admits only with 'the business justification AND the migration path "
                "away from the dependency' — add a substantive migration_path"
            )
        if tier != "C" and migration_path:
            raise PolicyError(
                f"{path}: declaration {entry['package']!r} sets migration_path but is "
                f"tier {tier!r}; that field belongs only to Tier C waivers"
            )
        name = _normalize(str(entry["package"]))
        if name in out:
            raise PolicyError(f"{path}: duplicate declaration for {entry['package']!r}")
        out[name] = Declaration(
            package=str(entry["package"]),
            license_id=str(entry["license"]),
            tier=tier,
            justification=justification,
            migration_path=migration_path,
        )
    return out


def _normalize(name: str) -> str:
    """PEP 503 name normalization, so `foo.bar_baz` and `foo-bar-baz` match."""
    return re.sub(r"[-_.]+", "-", name).lower()


def scan(ignore: frozenset[str] = frozenset()) -> list[Resolved]:
    """Resolve every installed distribution to a tier, sorted by name."""
    seen: dict[str, Resolved] = {}
    for dist in distributions():
        name = dist.metadata.get("Name")
        if not name or _normalize(name) in ignore:
            continue
        key = _normalize(name)
        if key in seen:  # same dist visible on multiple sys.path entries
            continue
        license_text, source = resolve_license(dist)
        seen[key] = Resolved(
            package=name,
            version=dist.metadata.get("Version") or "?",
            license_text=license_text or "<undeclared>",
            tier=classify(license_text, prose=source.startswith("License-File")),
            source=source,
        )
    return sorted(seen.values(), key=lambda r: r.package.lower())


def evaluate(
    resolved: list[Resolved], declarations: dict[str, Declaration]
) -> tuple[list[str], list[str]]:
    """Return ``(blocking, noted)`` messages for a resolved set."""
    blocking: list[str] = []
    noted: list[str] = []
    for item in resolved:
        declaration = declarations.get(_normalize(item.package))

        # A declaration states the human-verified license and overrides the
        # distribution's own metadata, which is sometimes simply wrong (owlrl
        # ships the W3C permissive license but classifies itself
        # "Other/Proprietary License"). The declared tier was already checked
        # against the declared license at load time, so this cannot be used to
        # launder a Tier C dependency into passing.
        tier = declaration.tier if declaration else item.tier

        if tier == "A":
            if declaration is not None:
                noted.append(
                    f"declared A (metadata corrected): {item.label} — {declaration.justification}"
                )
            continue
        # Tier D is forbidden outright (ADR-0024 §Tier D) — the one verdict no
        # declaration can reach, because "D" is not a declarable tier.
        if tier == "D":
            blocking.append(
                f"Tier D (forbidden by ADR-0024 — field-of-use restriction): {item.label}"
                + ("  [declaration ignored — Tier D has no waiver path]" if declaration else "")
            )
            continue
        # Tier B, C, or unknown: allowed only when declared. Tier C additionally
        # required a migration path at load time.
        if declaration is None:
            hint = (
                " — ADR-0024 §Tier C admits this only with an ADR giving the business "
                "justification and the migration path away from it"
                if tier == "C"
                else ""
            )
            blocking.append(
                f"Tier {tier} requires a declaration in .license-policy.toml: {item.label}{hint}"
            )
        else:
            suffix = (
                f"  [migration path: {declaration.migration_path}]"
                if declaration.migration_path
                else ""
            )
            noted.append(f"declared {tier}: {item.label} — {declaration.justification}{suffix}")
    return blocking, noted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path(".license-policy.toml"),
        help="path to the declaration file (default: .license-policy.toml)",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=[],
        help="distribution to skip (repeatable); use for the project itself",
    )
    parser.add_argument(
        "--list", action="store_true", help="print every resolved license and exit 0"
    )
    args = parser.parse_args(argv)

    try:
        declarations = load_declarations(args.policy)
    except PolicyError as exc:
        print(f"license-gate: {exc}", file=sys.stderr)
        return 2

    resolved = scan(ignore=frozenset(_normalize(n) for n in args.ignore))
    if args.list:
        for item in resolved:
            print(f"{item.tier}  {item.label}")
        return 0

    blocking, noted = evaluate(resolved, declarations)

    for line in noted:
        print(f"license-gate: {line}")
    counts = {t: sum(1 for r in resolved if r.tier == t) for t in ("A", "B", "C", "D", "unknown")}
    print(
        f"license-gate: {len(resolved)} distributions — "
        f"A={counts['A']} B={counts['B']} C={counts['C']} D={counts['D']} "
        f"unknown={counts['unknown']}"
    )

    if blocking:
        print(f"\nlicense-gate: {len(blocking)} policy violation(s) (ADR-0024):", file=sys.stderr)
        for line in blocking:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\nTier B / unknown: add a [[declaration]] to .license-policy.toml with a "
            "justification.\nTier C: file an ADR with the business justification and the "
            "migration path, then declare it with a migration_path.\nTier D: not waivable — "
            "remove the dependency or replace it with a Tier-A alternative.",
            file=sys.stderr,
        )
        return 1

    print("license-gate: OK — every dependency is Tier A or declared.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
