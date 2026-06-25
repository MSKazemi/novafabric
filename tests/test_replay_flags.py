from __future__ import annotations

import pytest

from novafabric.replay._flags import ReplayFlags


@pytest.mark.parametrize("mutation_class,expected", [
    ("none", True),
    ("read-only", False),
    ("idempotent-write", False),
    ("non-idempotent-write", False),
    ("external-side-effect", False),
    ("unknown", False),
])
def test_default_flags_permits_only_none(mutation_class: str, expected: bool) -> None:
    flags = ReplayFlags()
    assert flags.permits(mutation_class) == expected


@pytest.mark.parametrize("mutation_class,expected", [
    ("none", True),
    ("read-only", True),
    ("idempotent-write", False),
    ("non-idempotent-write", False),
    ("external-side-effect", False),
    ("unknown", False),
])
def test_allow_readonly_permits(mutation_class: str, expected: bool) -> None:
    flags = ReplayFlags(allow_readonly=True)
    assert flags.permits(mutation_class) == expected


@pytest.mark.parametrize("mutation_class,expected", [
    ("none", True),
    ("read-only", True),
    ("idempotent-write", True),
    ("non-idempotent-write", True),
    ("external-side-effect", False),
    ("unknown", False),
])
def test_allow_mutating_permits(mutation_class: str, expected: bool) -> None:
    flags = ReplayFlags(allow_mutating=True)
    assert flags.permits(mutation_class) == expected


@pytest.mark.parametrize("mutation_class,expected", [
    ("none", True),
    ("read-only", True),
    ("idempotent-write", True),
    ("non-idempotent-write", True),
    ("external-side-effect", True),
    ("unknown", False),
])
def test_allow_external_permits(mutation_class: str, expected: bool) -> None:
    flags = ReplayFlags(allow_external_side_effects=True)
    assert flags.permits(mutation_class) == expected


def test_allow_unknown_mutation_permits_all() -> None:
    flags = ReplayFlags(allow_unknown_mutation=True)
    for cls in ("none", "read-only", "idempotent-write", "non-idempotent-write",
                "external-side-effect", "unknown"):
        assert flags.permits(cls)


def test_active_flag_names_default() -> None:
    flags = ReplayFlags()
    assert flags.active_flag_names() == ["--mock-tools"]


def test_active_flag_names_dry_run() -> None:
    flags = ReplayFlags(dry_run=True)
    assert "--dry-run" in flags.active_flag_names()


def test_active_flag_names_allow_mutating() -> None:
    flags = ReplayFlags(allow_mutating=True)
    names = flags.active_flag_names()
    assert "--allow-mutating" in names


def test_unrecognized_mutation_class_denied() -> None:
    flags = ReplayFlags(allow_mutating=True)
    assert not flags.permits("totally-made-up-class")
