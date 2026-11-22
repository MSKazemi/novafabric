"""ADR-0306 Q7 (Option 1): the process invocation a framework adapter records.

An adapter capsule's ``command`` is a ``@framework:name`` label. To make such a
capsule re-runnable, every adapter now also records how its *process* was
started -- the ``process_invocation`` facet -- built by one helper in
``novafabric.capture.process_invocation``. These tests pin the helper's pure
parts (entry classification, home normalisation, the run ordinal), the facet's
shape (golden fixtures, valid and invalid), and that the facet reaches a real
capsule through the manifest redaction pass.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from pydantic import ValidationError

from novafabric.capture import process_invocation as pi

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "process-invocation"
SCHEMA = json.loads((REPO / "src" / "novafabric" / "schemas" / "run-capsule.schema.json").read_text())


@pytest.fixture(autouse=True)
def fresh_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts the per-process adapter-run count from zero."""
    monkeypatch.setattr(pi, "_COUNTER", pi.RunCounter())
    for var in ("NOVAFABRIC_REPLAY_MODE", "NOVAFABRIC_REPLAY_EVENTS_PATH", pi.ADAPTER_RUNS_ENV):
        monkeypatch.delenv(var, raising=False)


# --- entry classification ---------------------------------------------------------


@pytest.mark.parametrize(
    ("orig_argv", "argv", "expected"),
    [
        (["python3", "agent.py", "x"], ["agent.py", "x"], ("script", "agent.py")),
        (["python3", "-u", "-X", "dev", "agent.py"], ["agent.py"], ("script", "agent.py")),
        (["python3", "-Wignore", "agent.py"], ["agent.py"], ("script", "agent.py")),
        (["python3", "-m", "demo", "--x", "1"], ["/abs/demo/__main__.py", "--x", "1"],
         ("module", "demo")),
        (["python3", "-mdemo.cli"], ["/abs/demo/cli.py"], ("module", "demo.cli")),
        (["python3", "-c", "print(1)", "arg"], ["-c", "arg"], ("code", "print(1)")),
        (["python3", "-Bc", "print(1)"], ["-c"], ("code", "print(1)")),
        (["python3"], [""], ("interactive", None)),
        (["python3", "-"], ["-"], ("stdin", None)),
        # sys.argv disagrees with orig_argv: something rewrote it -> not reproducible.
        (["python3", "agent.py"], ["other.py"], ("unknown", None)),
        (["python3", "-m", "demo"], ["-c"], ("unknown", None)),
        (["python3", "-c"], ["-c"], ("unknown", None)),
        ([], [], ("unknown", None)),
    ],
)
def test_classify_entry(orig_argv: list[str], argv: list[str], expected: tuple[str, Any]) -> None:
    assert pi.classify_entry(orig_argv, argv, interactive=False, frozen=False) == expected


def test_an_interactive_or_frozen_process_is_never_a_script() -> None:
    assert pi.classify_entry(["python3", "-i", "a.py"], ["a.py"], interactive=True,
                             frozen=False) == ("interactive", None)
    assert pi.classify_entry(["app", "a.py"], ["a.py"], interactive=False,
                             frozen=True) == ("unknown", None)


# --- the facet built from a process ---------------------------------------------


def _build(tmp_path: Path, **overrides: Any) -> pi.ProcessInvocation:
    home = tmp_path / "home"
    proj = home / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    script = proj / "agent.py"
    script.write_text("print('hi')\n")
    kwargs: dict[str, Any] = {
        "ordinal": 1,
        "orig_argv": ["python3", "agent.py", str(proj / "in.txt")],
        "argv": ["agent.py", str(proj / "in.txt")],
        "executable": str(home / ".venv" / "bin" / "python"),
        "cwd": proj,
        "home": home,
        "main_file": str(script),
        "loaded_modules": ["json", "langgraph"],
        "stdin_isatty": False,
        "interactive": False,
        "frozen": False,
    }
    kwargs.update(overrides)
    return pi.build_invocation(**kwargs)


def test_a_script_invocation_is_recorded_home_normalised(tmp_path: Path) -> None:
    inv = _build(tmp_path)
    assert inv.argv == ["~/.venv/bin/python", "agent.py", "~/proj/in.txt"]
    assert inv.entry_kind == "script"
    assert inv.entry == "agent.py"
    assert inv.entry_file == "~/proj/agent.py"
    assert inv.cwd == "~/proj"
    import hashlib

    assert inv.entry_sha256 == "sha256:" + hashlib.sha256(b"print('hi')\n").hexdigest()
    assert inv.adapter_run_ordinal == 1
    assert inv.host_modules == []
    assert inv.stdin_isatty is False


def test_argv0_is_the_real_interpreter_not_the_one_typed(tmp_path: Path) -> None:
    inv = _build(tmp_path, orig_argv=["python3", "agent.py"], argv=["agent.py"],
                 executable="/opt/py/bin/python3.12")
    assert inv.argv[0] == "/opt/py/bin/python3.12"


def test_a_harness_host_module_is_recorded(tmp_path: Path) -> None:
    inv = _build(tmp_path, loaded_modules=["ipykernel", "json", "_pytest"])
    assert inv.host_modules == ["_pytest", "ipykernel"]


def test_a_relative_script_that_does_not_resolve_from_cwd_is_not_reproducible(
    tmp_path: Path,
) -> None:
    """The program changed directory before the adapter ran: the recorded cwd
    would not find the script again."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    inv = _build(tmp_path, cwd=elsewhere)
    assert inv.entry_kind == "unknown"


def test_a_module_entry_is_digested_from_main(tmp_path: Path) -> None:
    pkg = tmp_path / "home" / "proj" / "demo"
    pkg.mkdir(parents=True)
    (pkg / "__main__.py").write_text("x = 1\n")
    inv = _build(tmp_path, orig_argv=["python3", "-m", "demo"], argv=[str(pkg / "__main__.py")],
                 main_file=str(pkg / "__main__.py"))
    assert inv.entry_kind == "module"
    assert inv.entry == "demo"
    assert inv.entry_file == "~/proj/demo/__main__.py"
    assert inv.entry_sha256 is not None and inv.entry_sha256.startswith("sha256:")


def test_code_has_no_entry_file(tmp_path: Path) -> None:
    inv = _build(tmp_path, orig_argv=["python3", "-c", "import x"], argv=["-c"], main_file=None)
    assert inv.entry_kind == "code"
    assert inv.entry == "import x"
    assert inv.entry_file is None and inv.entry_sha256 is None


def test_current_invocation_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**_: Any) -> Any:
        raise RuntimeError("unreadable")

    monkeypatch.setattr(pi, "build_invocation", boom)
    assert pi.current_invocation(1) is None


def test_current_invocation_in_this_process_names_pytest_as_the_host() -> None:
    inv = pi.current_invocation(1)
    assert inv is not None
    assert "_pytest" in inv.host_modules


# --- the run ordinal --------------------------------------------------------------


def test_the_ordinal_counts_adapter_runs_in_one_process() -> None:
    counter = pi.RunCounter()
    assert [counter.next(), counter.next(), counter.next()] == [1, 2, 3]


def test_the_ordinal_restarts_in_a_forked_child() -> None:
    counter = pi.RunCounter()
    counter.next()
    counter.next()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        os.close(read)
        os.write(write, str(counter.next()).encode())
        os._exit(0)
    os.close(write)
    child = os.read(read, 16).decode()
    os.waitpid(pid, 0)
    assert child == "1"
    assert counter.next() == 3


def test_begin_adapter_run_counts_and_snapshots() -> None:
    first = pi.begin_adapter_run("langgraph")
    second = pi.begin_adapter_run("crewai")
    assert (first.ordinal, second.ordinal) == (1, 2)
    assert first.replaying is False
    assert first.facet is not None and first.facet["adapter_run_ordinal"] == 1
    assert second.facet is not None and second.facet["adapter_run_ordinal"] == 2


# --- the facet's shape (golden fixtures) -------------------------------------------


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("valid-*.json")))
def test_valid_fixtures_parse_and_validate_in_a_capsule(name: str) -> None:
    body = json.loads((FIXTURES / name).read_text())
    pi.ProcessInvocation.model_validate(body)
    capsule = json.loads(
        (REPO / "tests" / "fixtures" / "model-provenance" / "valid-text-only-capsule.json")
        .read_text()
    )
    capsule["facets"] = {pi.FACET_NAME: body}
    jsonschema.validate(capsule, SCHEMA)


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("invalid-*.json")))
def test_invalid_fixtures_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        pi.ProcessInvocation.model_validate(json.loads((FIXTURES / name).read_text()))


def test_the_fixture_sets_are_not_empty() -> None:
    assert len(list(FIXTURES.glob("valid-*.json"))) >= 2
    assert len(list(FIXTURES.glob("invalid-*.json"))) >= 4


def test_the_facet_is_registered_in_every_schema_copy() -> None:
    for path in (
        REPO / "src" / "novafabric" / "schemas" / "run-capsule.schema.json",
        REPO / "schemas" / "run-capsule.schema.json",
        REPO / "ui" / "dashboard" / "src" / "data" / "schemas" / "run-capsule.schema.json",
    ):
        registry = json.loads(path.read_text())["properties"]["facets"]["properties"]
        assert pi.FACET_NAME in registry, path


def test_attach_facet_is_additive() -> None:
    manifest: dict[str, Any] = {"facets": {"safety": {"schema_version": "0.1.0"}}}
    pi.attach_facet(manifest, {"schema_version": "0.1.0"})
    assert set(manifest["facets"]) == {"safety", pi.FACET_NAME}
    untouched: dict[str, Any] = {}
    pi.attach_facet(untouched, None)
    assert untouched == {}


# --- through a real adapter capsule ------------------------------------------------


def test_an_adapter_capsule_carries_the_facet_and_a_secret_argv_element_is_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk-proj-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0"
    monkeypatch.setattr(sys, "orig_argv", [sys.executable, "agent.py", f"--api-key={secret}"])
    monkeypatch.setattr(sys, "argv", ["agent.py", f"--api-key={secret}"])
    from novafabric.adapters._capsule import begin_capture

    cap = begin_capture(framework="langgraph", run_name="demo", data_dir=tmp_path / "caps")
    cap.finish()
    text = (cap.cap_dir / "capsule.yaml").read_text()
    assert secret not in text
    manifest = yaml.safe_load(text)
    facet = manifest["facets"][pi.FACET_NAME]
    assert facet["argv"][1] == "agent.py"
    assert "[REDACTED:" in facet["argv"][2]
    assert facet["adapter_run_ordinal"] == 1
    # The label and the capture mode are unchanged for every existing reader.
    assert manifest["command"] == ["@langgraph:demo"]
    assert manifest["capture_mode"] == "sdk-decorator"
    jsonschema.validate(manifest, SCHEMA)
    proof = json.loads((cap.cap_dir / "redaction-proof.json").read_text())
    assert any(f.get("target_ref") == "capsule.yaml" for f in proof.get("findings", []))


def test_under_mocked_replay_an_adapter_writes_no_capsule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("NOVAFABRIC_REPLAY_MODE", "mocked")
    monkeypatch.setenv("NOVAFABRIC_REPLAY_EVENTS_PATH", str(events))
    from novafabric.adapters._capsule import begin_capture

    cap = begin_capture(framework="crewai", run_name="demo", data_dir=tmp_path / "caps")
    cap.finish()
    assert not (tmp_path / "caps").exists() or not any((tmp_path / "caps").iterdir())
    lines = [json.loads(line) for line in events.read_text().splitlines()]
    assert [e["event"] for e in lines] == ["adapter_run", "adapter_run_end"]
    assert lines[0]["ordinal"] == 1 and lines[0]["framework"] == "crewai"
    assert lines[1]["exit_code"] == 0


def test_a_run_beyond_the_recorded_one_diverges_under_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.replay._errors import AdapterRunCountMismatchError

    events = tmp_path / "events.jsonl"
    monkeypatch.setenv("NOVAFABRIC_REPLAY_MODE", "mocked")
    monkeypatch.setenv("NOVAFABRIC_REPLAY_EVENTS_PATH", str(events))
    monkeypatch.setenv(pi.ADAPTER_RUNS_ENV, "1")
    monkeypatch.setenv("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY", "fail")
    pi.begin_adapter_run("langgraph")
    with pytest.raises(AdapterRunCountMismatchError) as exc:
        pi.begin_adapter_run("langgraph")
    assert exc.value.kind == "adapter_run_count_mismatch"
    divergences = [
        json.loads(line) for line in events.read_text().splitlines()
        if json.loads(line)["event"] == "divergence"
    ]
    assert [d["kind"] for d in divergences] == ["adapter_run_count_mismatch"]

    # Permissive: reported, not raised.
    monkeypatch.setenv("NOVAFABRIC_REPLAY_DIVERGENCE_POLICY", "warn")
    third = pi.begin_adapter_run("langgraph")
    assert third.replaying is True and third.ordinal == 3
