"""The ``replay_mutating`` policy gate covers every write-permitting ladder flag.

Acceptance criteria:

- ``ReplayFlags.permits_writes`` is the single answer to "may this replay run a
  side-effecting tool live?": True whenever any write-permitting ``allow_*`` flag
  is set (``--allow-mutating``, ``--allow-external-side-effects``,
  ``--allow-unknown-mutation``), False for the default and ``--allow-readonly``;
- every ``allow_*`` field on ``ReplayFlags`` is classified, exactly once, as
  read-only or write-permitting, and the classification agrees with
  ``permits()``. An ``allow_*`` field nobody classified counts as
  write-permitting (fail closed), and the guard below fails until it is
  classified;
- ``ReplayEngine.run()`` evaluates ``replay_mutating`` for each write-permitting
  flag *alone*, and a deny refuses the replay before anything runs — through the
  engine and through ``nova replay``; an allow lets it proceed;
- a policy engine that errors refuses the replay too (fail closed);
- read-only replays (no flag, ``--allow-readonly``) never consult the gate;
- no module outside ``replay/_flags.py`` reads a single write flag, so a gate
  cannot again be keyed on one rung of the ladder.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, fields
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.policy import PolicyDecision, PolicyDeniedError
from novafabric.replay import ReplayEngine, ReplayFlags, _flags

runner = CliRunner()

#: The write-permitting rungs, as (dataclass field, CLI flag). Stated here
#: independently of the code under test.
WRITE_FLAGS: list[tuple[str, str]] = [
    ("allow_mutating", "--allow-mutating"),
    ("allow_external_side_effects", "--allow-external-side-effects"),
    ("allow_unknown_mutation", "--allow-unknown-mutation"),
]
WRITE_FIELDS = [name for name, _ in WRITE_FLAGS]

#: ReplayFlags fields that grant no tool permission. A new field must be added
#: here or to one of the ``allow_*`` classifications in ``_flags.py``.
NON_PERMISSION_FIELDS = {
    "mode",
    "dry_run",
    "output_dir",
    "intervention_file",
    "required_environment",
    "permissive",
}

_WRITE_CLASSES = {"idempotent-write", "non-idempotent-write", "external-side-effect", "unknown"}


def _capsule(tmp_path: Path) -> Path:
    capsule = tmp_path / "capsule"
    capsule.mkdir()
    manifest = {"run_id": "run-gate-1", "command": ["true"]}
    (capsule / "capsule.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return capsule


def _engine(monkeypatch: pytest.MonkeyPatch, decision: PolicyDecision) -> MagicMock:
    engine = MagicMock()
    engine.evaluate.return_value = decision
    monkeypatch.setattr("novafabric.replay._engine.get_policy_engine", lambda: engine)
    return engine


@pytest.fixture
def audit_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("NOVAFABRIC_AUDIT_LOG_PATH", str(path))
    return path


@pytest.fixture
def deny_all(monkeypatch: pytest.MonkeyPatch, audit_log: Path) -> MagicMock:
    return _engine(
        monkeypatch, PolicyDecision(allow=False, reason="deny-all", decision_id="deny-1")
    )


@pytest.fixture
def allow_all(monkeypatch: pytest.MonkeyPatch, audit_log: Path) -> MagicMock:
    return _engine(monkeypatch, PolicyDecision(allow=True, reason="ok", decision_id="allow-1"))


# ── the property ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", WRITE_FIELDS)
def test_each_write_flag_alone_permits_writes(name: str) -> None:
    assert ReplayFlags(**{name: True}).permits_writes is True


@pytest.mark.parametrize(
    "flags", [ReplayFlags(), ReplayFlags(allow_readonly=True)], ids=["default", "readonly"]
)
def test_read_only_flags_do_not_permit_writes(flags: ReplayFlags) -> None:
    assert flags.permits_writes is False


def test_non_permission_fields_do_not_permit_writes() -> None:
    flags = ReplayFlags(mode="exact", dry_run=True, permissive=True)
    assert flags.permits_writes is False


# ── classification guard ────────────────────────────────────────────────────


def test_every_allow_field_is_classified_exactly_once() -> None:
    allow_fields = {f.name for f in fields(ReplayFlags) if f.name.startswith("allow_")}
    read_only, writes = _flags.READ_ONLY_ALLOW_FLAGS, _flags.WRITE_PERMITTING_ALLOW_FLAGS
    assert read_only.isdisjoint(writes)
    unclassified = allow_fields - read_only - writes
    assert not unclassified, (
        f"ReplayFlags has unclassified allow_* fields {sorted(unclassified)}: add each to "
        "READ_ONLY_ALLOW_FLAGS or WRITE_PERMITTING_ALLOW_FLAGS in replay/_flags.py"
    )
    stale = (read_only | writes) - allow_fields
    assert not stale, f"classified names that are not ReplayFlags fields: {sorted(stale)}"


def test_every_field_is_either_a_classified_permission_or_listed_as_none() -> None:
    names = {f.name for f in fields(ReplayFlags)}
    classified = (
        _flags.READ_ONLY_ALLOW_FLAGS | _flags.WRITE_PERMITTING_ALLOW_FLAGS | NON_PERMISSION_FIELDS
    )
    assert names == classified, (
        f"unclassified ReplayFlags fields: {sorted(names - classified)}; "
        f"listed but absent: {sorted(classified - names)}"
    )


def test_write_classification_matches_the_independent_list() -> None:
    assert _flags.WRITE_PERMITTING_ALLOW_FLAGS == set(WRITE_FIELDS)
    assert _flags.READ_ONLY_ALLOW_FLAGS == {"allow_readonly"}


@pytest.mark.parametrize("name", [*WRITE_FIELDS, "allow_readonly"])
def test_classification_agrees_with_permits(name: str) -> None:
    flags = ReplayFlags(**{name: True})
    grants_a_write_class = any(flags.permits(c) for c in _WRITE_CLASSES)
    assert grants_a_write_class == (name in _flags.WRITE_PERMITTING_ALLOW_FLAGS)


def test_an_unclassified_allow_field_fails_closed() -> None:
    """A rung added without classifying it must still trip the gate."""

    @dataclass
    class FutureFlags(ReplayFlags):
        allow_something_new: bool = False

    assert FutureFlags().permits_writes is False
    assert FutureFlags(allow_something_new=True).permits_writes is True


def test_no_module_reads_a_single_write_flag_outside_flags() -> None:
    """One property, asserted once: every gate keys on ``permits_writes``."""
    src = Path(__file__).resolve().parents[2] / "src" / "novafabric"
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path == src / "replay" / "_flags.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in WRITE_FIELDS:
                offenders.append(f"{path.relative_to(src)}:{node.lineno} .{node.attr}")
    assert not offenders, (
        "read ReplayFlags.permits_writes instead of a single write flag: " + ", ".join(offenders)
    )


# ── engine ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("dry_run", [True, False], ids=["dry-run", "run"])
@pytest.mark.parametrize("name", WRITE_FIELDS)
def test_engine_refuses_each_write_flag_under_a_deny_policy(
    tmp_path: Path, deny_all: MagicMock, audit_log: Path, name: str, dry_run: bool
) -> None:
    out = tmp_path / "out"
    flags = ReplayFlags(dry_run=dry_run, **{name: True})
    with pytest.raises(PolicyDeniedError, match="deny-all"):
        ReplayEngine(_capsule(tmp_path), flags, base_dir=out).run()
    (inp,), _ = deny_all.evaluate.call_args
    assert inp.action == "replay_mutating"
    assert not out.exists(), "a refused replay must not write a replay directory"
    entries = [json.loads(line) for line in audit_log.read_text().splitlines()]
    assert any(
        e.get("event_type") == "policy.deny" and e["details"]["action"] == "replay_mutating"
        for e in entries
    ), entries


@pytest.mark.parametrize("name", WRITE_FIELDS)
def test_engine_proceeds_for_each_write_flag_under_an_allow_policy(
    tmp_path: Path, allow_all: MagicMock, name: str
) -> None:
    flags = ReplayFlags(dry_run=True, **{name: True})
    result = ReplayEngine(_capsule(tmp_path), flags, base_dir=tmp_path / "out").run()
    assert result.status == "dry_run"
    allow_all.evaluate.assert_called_once()
    (inp,), _ = allow_all.evaluate.call_args
    assert inp.action == "replay_mutating"


@pytest.mark.parametrize("name", WRITE_FIELDS)
def test_engine_fails_closed_when_the_policy_engine_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_log: Path, name: str
) -> None:
    engine = MagicMock()
    engine.evaluate.side_effect = RuntimeError("opa crashed")
    monkeypatch.setattr("novafabric.replay._engine.get_policy_engine", lambda: engine)
    out = tmp_path / "out"
    with pytest.raises(RuntimeError, match="opa crashed"):
        ReplayEngine(_capsule(tmp_path), ReplayFlags(dry_run=True, **{name: True}), out).run()
    assert not out.exists()


@pytest.mark.parametrize("mode", ["mocked", "forensic", "semantic"])
@pytest.mark.parametrize(
    "extra", [{}, {"allow_readonly": True}], ids=["no-flag", "allow-readonly"]
)
def test_read_only_replays_never_consult_the_gate(
    tmp_path: Path, deny_all: MagicMock, mode: str, extra: dict[str, bool]
) -> None:
    flags = ReplayFlags(mode=mode, dry_run=True, **extra)  # type: ignore[arg-type]
    ReplayEngine(_capsule(tmp_path), flags, base_dir=tmp_path / "out").run()
    deny_all.evaluate.assert_not_called()


# ── CLI ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cli_flag", [flag for _, flag in WRITE_FLAGS])
def test_cli_refuses_each_write_flag_under_a_deny_policy(
    tmp_path: Path, deny_all: MagicMock, cli_flag: str
) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["replay", str(_capsule(tmp_path)), "--dry-run", cli_flag, "-o", str(out)]
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, PolicyDeniedError), result.output
    deny_all.evaluate.assert_called_once()
    assert not out.exists()


@pytest.mark.parametrize("cli_flag", [flag for _, flag in WRITE_FLAGS])
def test_cli_proceeds_for_each_write_flag_under_an_allow_policy(
    tmp_path: Path, allow_all: MagicMock, cli_flag: str
) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["replay", str(_capsule(tmp_path)), "--dry-run", cli_flag, "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    allow_all.evaluate.assert_called_once()


@pytest.mark.parametrize("args", [[], ["--allow-readonly"]], ids=["no-flag", "allow-readonly"])
def test_cli_read_only_replay_never_consults_the_gate(
    tmp_path: Path, deny_all: MagicMock, args: list[str]
) -> None:
    out = tmp_path / "out"
    result = runner.invoke(
        app, ["replay", str(_capsule(tmp_path)), "--dry-run", *args, "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    deny_all.evaluate.assert_not_called()
