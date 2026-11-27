"""Guard: docs/collector-reference.md names every flag and env var of the Go binaries.

The Go collector binaries define their flags with the standard ``flag`` package in
``collector/cmd/<binary>/main.go``. Their reference page drifted before: a flag
added in Go never reached the docs. This guard parses each ``main.go`` with a
regular expression (it does not build or run Go) and fails if a flag is missing
from the page as ``--<name>``, or an environment variable the binary reads is not
named on the page.

``novafabric-hpc-hub`` is excluded: it is a stub documented in
``docs/cli-reference.md``, not on the collector reference page.
``novafabric-collector`` defines no flags of its own (they come from the upstream
``otelcol`` command), so it contributes nothing to the scan.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CMD_DIR = REPO_ROOT / "collector" / "cmd"
REFERENCE = REPO_ROOT / "docs" / "collector-reference.md"

#: Binaries documented elsewhere, with the reason.
EXCLUDED = {"novafabric-hpc-hub": "stub, documented in docs/cli-reference.md"}

#: ``flag.String("name", ...)`` or ``fs.Bool("name", ...)`` and the other typed
#: constructors, plus the ``*Var`` forms whose name is the second argument.
_FLAG_CALL = re.compile(
    r"\b\w+\.(?:String|Bool|Int|Int64|Uint|Uint64|Float64|Duration|Func|BoolFunc)"
    r"\(\s*\"([A-Za-z0-9][A-Za-z0-9_-]*)\""
)
_FLAG_VAR_CALL = re.compile(
    r"\b\w+\.(?:StringVar|BoolVar|IntVar|Int64Var|UintVar|Uint64Var|Float64Var|DurationVar|Var|TextVar)"
    r"\(\s*[^,]+,\s*\"([A-Za-z0-9][A-Za-z0-9_-]*)\""
)
#: ``os.Getenv("X")``, ``os.LookupEnv("X")`` and the binaries' ``envOr("X", ...)``.
_ENV_CALL = re.compile(r"\b(?:os\.Getenv|os\.LookupEnv|envOr)\(\s*\"([A-Z][A-Z0-9_]*)\"")


def flags_in(source: str) -> set[str]:
    """Return the flag names a Go source defines with the ``flag`` package."""
    return set(_FLAG_CALL.findall(source)) | set(_FLAG_VAR_CALL.findall(source))


def env_vars_in(source: str) -> set[str]:
    """Return the environment variables a Go source reads by name."""
    return set(_ENV_CALL.findall(source))


def _documented_binaries() -> list[Path]:
    return sorted(path for path in CMD_DIR.glob("*/main.go") if path.parent.name not in EXCLUDED)


def test_parser_finds_flags_and_env_vars_in_go_source() -> None:
    source = """
    spool := flag.String("spool", os.Getenv("NOVAFABRIC_SPOOL_DIR"), "dir")
    nats := flag.String("nats", envOr("NOVA_NATS_URL", "nats://x"), "url")
    every := flag.Duration("interval", time.Second, "d")
    wal := fs.Bool("local-wal", false, "dev key")
    flag.IntVar(&n, "max-events", 10, "n")
    """
    assert flags_in(source) == {"spool", "nats", "interval", "local-wal", "max-events"}
    assert env_vars_in(source) == {"NOVAFABRIC_SPOOL_DIR", "NOVA_NATS_URL"}


def test_parser_ignores_non_flag_calls() -> None:
    source = 'fmt.Println("spool")\nx := strings.Index("a", "b")\nos.Setenv("NOT_READ", "1")'
    assert flags_in(source) == set()
    assert env_vars_in(source) == set()


def test_scan_is_not_vacuous() -> None:
    """The scan must see the binaries and their flags, or it would pass on nothing."""
    binaries = {path.parent.name for path in _documented_binaries()}
    assert {"novafabric-spool-forwarder", "novafabric-verifier"} <= binaries
    total = sum(len(flags_in(path.read_text(encoding="utf-8"))) for path in _documented_binaries())
    assert total >= 10, (
        f"only {total} flags found in {sorted(binaries)}: has the flag style changed?"
    )


@pytest.mark.parametrize("main_go", _documented_binaries(), ids=lambda p: p.parent.name)
def test_every_flag_is_on_the_reference_page(main_go: Path) -> None:
    page = REFERENCE.read_text(encoding="utf-8")
    missing = sorted(
        name
        for name in flags_in(main_go.read_text(encoding="utf-8"))
        if not re.search(rf"`--{re.escape(name)}\b", page)
    )
    assert not missing, (
        f"{main_go.parent.name} defines flag(s) {missing} that docs/collector-reference.md "
        "does not document as `--<name>`. Add each one to that binary's flag table."
    )


@pytest.mark.parametrize("main_go", _documented_binaries(), ids=lambda p: p.parent.name)
def test_every_env_var_is_on_the_reference_page(main_go: Path) -> None:
    page = REFERENCE.read_text(encoding="utf-8")
    missing = sorted(
        name for name in env_vars_in(main_go.read_text(encoding="utf-8")) if f"`{name}" not in page
    )
    assert not missing, (
        f"{main_go.parent.name} reads environment variable(s) {missing} that "
        "docs/collector-reference.md does not name. Add each one to that binary's section."
    )


def test_reference_page_is_linked_from_the_docs_index() -> None:
    index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "(collector-reference.md)" in index
