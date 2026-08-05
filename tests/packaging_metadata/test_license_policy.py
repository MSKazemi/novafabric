"""Guard: the dependency tree obeys ADR-0024's license policy.

Closes ADR-0024's stated gap ("the CI enforcement step is not yet implemented;
manual review is the gap"). The end-to-end guard is
``test_installed_tree_satisfies_the_license_policy`` — everything else pins the
classifier behaviour that guard depends on, because a license gate that
misclassifies is worse than none: it manufactures false confidence.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / ".license-policy.toml"


def _load_gate():
    """Import scripts/license_gate.py by path — scripts/ is not a package."""
    path = REPO_ROOT / "scripts" / "license_gate.py"
    spec = importlib.util.spec_from_file_location("license_gate", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["license_gate"] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


# --------------------------------------------------------------------------
# Tier mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("license_string", "expected"),
    [
        ("MIT", "A"),
        ("Apache-2.0", "A"),
        ("Apache Software License", "A"),
        ("BSD-3-Clause", "A"),
        ("0BSD", "A"),
        ("Zlib", "A"),
        ("ISC License (ISCL)", "A"),
        ("PostgreSQL License", "A"),
        ("Public Domain", "A"),
        ("LGPL-3.0-only", "B"),
        ("GNU Lesser General Public License v2 or later (LGPLv2+)", "B"),
        ("MPL-2.0", "B"),
        ("GPL-3.0", "C"),
        ("GNU General Public License v2 or later (GPLv2+)", "C"),
        ("AGPL-3.0", "C"),
        ("SSPL-1.0", "C"),
        ("Business Source License 1.1", "C"),
        ("Elastic License 2.0", "C"),
        ("Other/Proprietary License", "C"),
        ("", "unknown"),
        ("Totally Bespoke Licence", "unknown"),
    ],
)
def test_classify_maps_licenses_to_adr0024_tiers(license_string: str, expected: str) -> None:
    assert gate.classify(license_string) == expected


@pytest.mark.parametrize(
    "license_string",
    [
        "The Software shall be used for Good, not Evil",
        "Hippocratic License 3.0",
        "Anti-996 License",
        "Community licence with a no military use clause",
        "Ethical Source licence",
    ],
)
def test_field_of_use_restrictions_are_tier_d(license_string: str) -> None:
    """ADR-0024 §Tier D: field-of-use and "ethical source" terms break OSI
    compliance and create distribution uncertainty. Unlike Tier C they are
    forbidden outright — no ADR can waive them."""
    assert gate.classify(license_string) == "D"


def test_tier_d_outranks_tier_c_in_a_conjunction() -> None:
    """A conjunction binds every term, so the most severe one governs."""
    assert gate.classify("MIT AND Hippocratic License") == "D"
    assert gate.classify("GPL-3.0 AND Hippocratic License") == "D"


def test_lgpl_is_not_swallowed_by_the_gpl_rule() -> None:
    """Rule order matters: 'Lesser General Public License' contains 'General
    Public License', so LGPL must be tested before GPL or every Tier B
    copyleft dep would be misreported as a Tier C block."""
    assert gate.classify("GNU Lesser General Public License") == "B"
    assert gate.classify("GNU General Public License") == "C"


# --------------------------------------------------------------------------
# Multi-license expressions
# --------------------------------------------------------------------------


def test_disjunction_takes_the_most_permissive_arm() -> None:
    """pyphen is tri-licensed GPLv2+ / LGPLv2+ / MPL-1.1. The consumer chooses,
    so it is Tier B via LGPL — not Tier C via GPL. A naive `grep GPL` (which is
    what the Go collector gate does) false-positives exactly here."""
    assert gate.classify("GPL-2.0+ OR LGPL-2.1+ OR MPL-1.1") == "B"
    assert gate.classify("MIT OR Apache-2.0") == "A"


def test_conjunction_takes_the_least_permissive_term() -> None:
    """tqdm is 'MPL-2.0 AND MIT' — both bind, so the stricter one governs."""
    assert gate.classify("MPL-2.0 AND MIT") == "B"
    assert gate.classify("BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0") == "A"


def test_slash_is_not_a_disjunction() -> None:
    """Trove classifiers use '/' inside a single name. Treating it as OR made
    'Other/Proprietary License' resolve to the more permissive 'Other'
    (unknown) instead of blocking as proprietary."""
    assert gate.classify("Other/Proprietary License") == "C"


# --------------------------------------------------------------------------
# Prose vs expression
# --------------------------------------------------------------------------


def test_license_file_prose_is_not_parsed_as_an_expression() -> None:
    """Apache-2.0's own text contains 'USE, REPRODUCTION, AND DISTRIBUTION'.
    Parsed as an SPDX expression, that 'AND' splits the body into fragments
    that resolve to unknown — so a correctly-licensed package would block.
    Modern wheels (google-crc32c, sigstore-models) declare a license *only*
    as a file, so this path has to be right."""
    apache_head = (
        "Apache License Version 2.0, January 2004 http://www.apache.org/licenses/ "
        "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION 1. Definitions."
    )
    assert gate.classify(apache_head, prose=True) == "A"
    assert gate.classify(apache_head, prose=False) == "unknown"

    mit_head = (
        "MIT License Copyright (c) 2025 Permission is hereby granted, free of charge, "
        "to any person obtaining a copy of this software and associated documentation "
        "files to deal in the Software without restriction"
    )
    assert gate.classify(mit_head, prose=True) == "A"


def test_prose_rule_order_still_blocks_copyleft_text() -> None:
    """Deny-first ordering means a GPL body cannot be downgraded by permissive
    words appearing later in it."""
    gpl_head = "GNU GENERAL PUBLIC LICENSE Version 3 everyone is permitted to copy and distribute"
    assert gate.classify(gpl_head, prose=True) == "C"


# --------------------------------------------------------------------------
# Declaration file validation
# --------------------------------------------------------------------------


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".license-policy.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_missing_policy_file_is_not_an_error(tmp_path: Path) -> None:
    assert gate.load_declarations(tmp_path / "absent.toml") == {}


def test_tier_d_cannot_be_declared(tmp_path: Path) -> None:
    """ADR-0024 §Tier D is "Forbidden" with no waiver, so a tier = "D" entry is
    malformed by construction. (Tier C, by contrast, IS waivable — §Tier C is
    titled "Reject by default; explicit waiver required".)"""
    path = _write(
        tmp_path,
        '[[declaration]]\npackage = "x"\nlicense = "Hippocratic License"\ntier = "D"\n'
        'justification = "we would really like to ship this anyway please"\n',
    )
    with pytest.raises(gate.PolicyError, match="no waiver path"):
        gate.load_declarations(path)


def test_tier_c_is_waivable_but_demands_a_migration_path(tmp_path: Path) -> None:
    """ADR-0024 §Tier C admits a dependency only with "the business
    justification AND the migration path away from the dependency"."""
    without = (
        '[[declaration]]\npackage = "x"\nlicense = "GPL-3.0"\ntier = "C"\n'
        'justification = "a business justification of entirely adequate length"\n'
    )
    with pytest.raises(gate.PolicyError, match="migration_path"):
        gate.load_declarations(_write(tmp_path, without))

    with_path = without + 'migration_path = "replace with the Tier-A fork tracked in ADR-9999"\n'
    loaded = gate.load_declarations(_write(tmp_path, with_path))
    assert loaded["x"].tier == "C"
    assert "ADR-9999" in loaded["x"].migration_path


def test_migration_path_is_rejected_on_non_tier_c(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[[declaration]]\npackage = "x"\nlicense = "MPL-2.0"\ntier = "B"\n'
        'justification = "a perfectly adequate justification string"\n'
        'migration_path = "this field belongs only to tier C waivers"\n',
    )
    with pytest.raises(gate.PolicyError, match="belongs only to Tier C"):
        gate.load_declarations(path)


def test_declaration_must_agree_with_its_own_license(tmp_path: Path) -> None:
    """Stops a declaration being used to launder a license into a friendlier
    tier: the tier is re-derived from the declared text, never trusted."""
    path = _write(
        tmp_path,
        '[[declaration]]\npackage = "x"\nlicense = "GPL-3.0"\ntier = "B"\n'
        'justification = "claiming a copyleft license is merely tier B"\n',
    )
    with pytest.raises(gate.PolicyError, match="classifies as 'C'"):
        gate.load_declarations(path)


@pytest.mark.parametrize("claimed_tier", ["A", "B", "unknown"])
def test_laundering_a_copyleft_license_is_rejected_at_load(
    tmp_path: Path, claimed_tier: str
) -> None:
    """This is the load-bearing invariant of the whole declaration mechanism.

    `evaluate()` trusts `declaration.tier`, so the only thing standing between
    a GPL dependency and a green gate is the loader re-deriving the tier from
    the declared license text. No claimed tier may launder a Tier C license."""
    path = _write(
        tmp_path,
        f'[[declaration]]\npackage = "x"\nlicense = "GPL-3.0"\ntier = "{claimed_tier}"\n'
        'justification = "a plausible-sounding justification of adequate length"\n',
    )
    with pytest.raises(gate.PolicyError):
        gate.load_declarations(path)


def test_placeholder_justification_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[[declaration]]\npackage = "x"\nlicense = "MPL-2.0"\ntier = "B"\njustification = "TODO"\n',
    )
    with pytest.raises(gate.PolicyError, match="real .*justification"):
        gate.load_declarations(path)


def test_unexpected_or_missing_keys_are_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        '[[declaration]]\npackage = "x"\nlicense = "MPL-2.0"\ntier = "B"\n'
        'justification = "a perfectly adequate justification string"\nexpires = "2099-01-01"\n',
    )
    with pytest.raises(gate.PolicyError, match="unexpected"):
        gate.load_declarations(path)


def test_duplicate_declarations_are_rejected(tmp_path: Path) -> None:
    entry = (
        '[[declaration]]\npackage = "x"\nlicense = "MPL-2.0"\ntier = "B"\n'
        'justification = "a perfectly adequate justification string"\n'
    )
    with pytest.raises(gate.PolicyError, match="duplicate"):
        gate.load_declarations(_write(tmp_path, entry * 2))


def test_malformed_toml_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(gate.PolicyError, match="malformed TOML"):
        gate.load_declarations(_write(tmp_path, "[[declaration]\nbroken"))


def test_package_names_are_pep503_normalized(tmp_path: Path) -> None:
    """`psycopg_pool` in a declaration must match `psycopg-pool` as installed."""
    path = _write(
        tmp_path,
        '[[declaration]]\npackage = "Psycopg_Pool"\nlicense = "LGPL-3.0-only"\ntier = "B"\n'
        'justification = "normalization check, adequately justified for the test"\n',
    )
    assert "psycopg-pool" in gate.load_declarations(path)


# --------------------------------------------------------------------------
# Evaluation semantics
# --------------------------------------------------------------------------


def _resolved(name: str, tier: str, license_text: str = "x") -> object:
    return gate.Resolved(
        package=name, version="1.0", license_text=license_text, tier=tier, source="test"
    )


def test_tier_d_blocks_with_and_without_a_declaration() -> None:
    """The one thing a declaration must never be able to do.

    Note where the guarantee actually lives: `evaluate()` trusts a
    declaration's tier, so what makes that sound is `load_declarations()`
    re-deriving the tier from the declared license text and rejecting any
    mismatch (see `test_laundering_a_copyleft_license_is_rejected_at_load`).
    This test pins the narrower property that a tier = "C" declaration is
    never honoured even if one reaches `evaluate()`."""
    item = _resolved("bad", "D", "Hippocratic License")

    blocking, _ = gate.evaluate([item], {})
    assert blocking and "Tier D" in blocking[0]

    # A declaration whose declared license is itself Tier C — constructed
    # directly, bypassing the loader's rejection, to prove evaluate() is not
    # relying on the loader to be the only guard.
    smuggled = {
        "bad": gate.Declaration(
            package="bad", license_id="Hippocratic License", tier="D", justification="x" * 25
        )
    }
    blocking, _ = gate.evaluate([item], smuggled)
    assert blocking and "Tier D" in blocking[0]
    assert "no waiver path" in blocking[0]


def test_undeclared_tier_b_blocks_and_declared_tier_b_passes() -> None:
    item = _resolved("dep", "B")
    blocking, _ = gate.evaluate([item], {})
    assert blocking and "requires a declaration" in blocking[0]

    declared = {
        "dep": gate.Declaration(
            package="dep", license_id="MPL-2.0", tier="B", justification="x" * 25
        )
    }
    blocking, noted = gate.evaluate([item], declared)
    assert not blocking
    assert noted and "declared B" in noted[0]


def test_declaration_corrects_wrong_upstream_metadata() -> None:
    """owlrl ships the W3C permissive license but classifies itself
    'Other/Proprietary License'. A verified declaration overrides the bad
    metadata; it still cannot rescue a genuinely Tier C dependency, because
    load_declarations() re-derives the tier from the declared license."""
    item = _resolved("owlrl", "C", "Other/Proprietary License")
    declared = {
        "owlrl": gate.Declaration(
            package="owlrl",
            license_id="W3C Software and Document Notice and License",
            tier="unknown",
            justification="upstream classifier is wrong; verified permissive",
        )
    }
    blocking, noted = gate.evaluate([item], declared)
    assert not blocking
    assert noted and "owlrl" in noted[0]


def test_tier_a_needs_no_paperwork() -> None:
    blocking, noted = gate.evaluate([_resolved("nice", "A")], {})
    assert not blocking and not noted


# --------------------------------------------------------------------------
# The actual guard
# --------------------------------------------------------------------------


def test_repo_policy_file_is_wellformed() -> None:
    declarations = gate.load_declarations(POLICY_PATH)
    assert declarations, "expected the repo's declarations to load"
    for name, declaration in declarations.items():
        assert declaration.tier in gate.VALID_DECLARED_TIERS
        assert len(declaration.justification) >= 20, name


def test_installed_tree_satisfies_the_license_policy() -> None:
    """Every installed distribution is Tier A, or Tier B/unknown with a
    recorded justification. This is the gate ADR-0024 asked for."""
    declarations = gate.load_declarations(POLICY_PATH)
    resolved = gate.scan(ignore=frozenset({"novafabric"}))
    assert len(resolved) > 50, "expected a populated environment"

    blocking, _ = gate.evaluate(resolved, declarations)
    assert not blocking, "ADR-0024 license policy violations:\n" + "\n".join(
        f"  - {line}" for line in blocking
    )


def test_no_declaration_is_stale() -> None:
    """A declaration for a package that is no longer installed is dead weight
    and hides the fact that a dependency was dropped."""
    installed = {gate._normalize(item.package) for item in gate.scan()}
    declared = set(gate.load_declarations(POLICY_PATH))
    assert not (declared - installed), (
        f"declarations for uninstalled packages: {sorted(declared - installed)}"
    )
