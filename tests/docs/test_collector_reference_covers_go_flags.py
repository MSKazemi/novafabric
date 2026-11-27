"""Guard: docs/collector-reference.md names every flag and env var of the Go binaries.

The Go collector binaries define their flags with the standard ``flag`` package in
``collector/cmd/<binary>/main.go``. Their reference page drifted before: a flag
added in Go never reached the docs. This guard parses the Go source with regular
expressions (it does not build or run Go) and fails when, inside that binary's own
``## <binary>`` section of the page:

* a flag the binary defines is not documented as the exact token ``--<name>``; or
* an environment variable the binary reads is not named. "Reads" covers ``main.go``
  and every non-test Go file of the collector-module packages it imports,
  transitively: ``os.Getenv``/``os.LookupEnv`` with a literal or a string constant,
  the binaries' ``envOr`` helper, and ``os.UserHomeDir`` (which reads ``HOME``).

``novafabric-hpc-hub`` is excluded: it is a stub documented in
``docs/cli-reference.md``, not on the collector reference page.
``novafabric-collector`` defines no flags of its own (they come from the upstream
``otelcol`` command), so it contributes env vars only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR = REPO_ROOT / "collector"
CMD_DIR = COLLECTOR / "cmd"
REFERENCE = REPO_ROOT / "docs" / "collector-reference.md"
MODULE = "github.com/MSKazemi/novafabric/collector"

#: Binaries documented elsewhere, with the reason.
EXCLUDED = {"novafabric-hpc-hub": "stub, documented in docs/cli-reference.md"}

_FLAG_TYPES = "String|Bool|Int|Int64|Uint|Uint64|Float64|Duration|Func|BoolFunc|TextVar"
_FLAG_VAR_TYPES = "StringVar|BoolVar|IntVar|Int64Var|UintVar|Uint64Var|Float64Var|DurationVar|Var"
#: ``fs := flag.NewFlagSet(...)`` / ``var fs = pflag.NewFlagSet(...)``: a FlagSet name.
_FLAGSET_DECL = re.compile(r"\b(\w+)\s*(?::=|=)\s*(?:flag|pflag)\.NewFlagSet\(")
#: ``os.Getenv("X")``, ``os.LookupEnv("X")`` and the binaries' ``envOr("X", ...)``.
_ENV_LITERAL = re.compile(r"\b(?:os\.Getenv|os\.LookupEnv|envOr)\(\s*\"([A-Z][A-Z0-9_]*)\"")
#: ``os.Getenv(envDLQDir)``: resolved through a string constant of the same package.
_ENV_IDENT = re.compile(r"\b(?:os\.Getenv|os\.LookupEnv)\(\s*([A-Za-z_]\w*)\s*\)")
_MODULE_IMPORT = re.compile(rf"\"{re.escape(MODULE)}/([\w./-]+)\"")


def _flag_receivers(source: str) -> list[str]:
    """The identifiers whose method calls define flags: the packages and every FlagSet."""
    return sorted({"flag", "pflag", *_FLAGSET_DECL.findall(source)})


def flags_in(source: str) -> set[str]:
    """Return the flag names a Go source defines with the ``flag`` (or ``pflag``) package.

    Only calls on ``flag.``/``pflag.`` or on a variable assigned from ``NewFlagSet``
    count, so ``slog.String("dir", ...)`` and similar helpers are not flags.
    """
    receivers = "|".join(re.escape(r) for r in _flag_receivers(source))
    direct = re.compile(
        rf"(?<![\w.])(?:{receivers})\.(?:{_FLAG_TYPES})\(\s*\"([A-Za-z0-9][A-Za-z0-9_-]*)\""
    )
    via_var = re.compile(
        rf"(?<![\w.])(?:{receivers})\.(?:{_FLAG_VAR_TYPES})"
        r"\(\s*[^,]+,\s*\"([A-Za-z0-9][A-Za-z0-9_-]*)\""
    )
    return set(direct.findall(source)) | set(via_var.findall(source))


def env_vars_in(source: str, package_source: str | None = None) -> set[str]:
    """Return the environment variables a Go source reads by name.

    ``package_source`` is the text of the whole package, used to resolve
    ``os.Getenv(someConst)`` to the constant's string value.
    """
    pkg = source if package_source is None else package_source
    found = set(_ENV_LITERAL.findall(source))
    for ident in _ENV_IDENT.findall(source):
        match = re.search(rf"\b{re.escape(ident)}\s*(?:string\s*)?=\s*\"([A-Z][A-Z0-9_]*)\"", pkg)
        if match:
            found.add(match.group(1))
    if re.search(r"\bos\.UserHomeDir\(", source):
        found.add("HOME")
    return found


def _go_files(package_dir: Path) -> list[Path]:
    return sorted(p for p in package_dir.glob("*.go") if not p.name.endswith("_test.go"))


def package_closure(main_go: Path) -> list[Path]:
    """The binary's package directory plus every collector-module package it imports."""
    seen: list[Path] = []
    queue = [main_go.parent]
    while queue:
        pkg = queue.pop()
        if pkg in seen:
            continue
        seen.append(pkg)
        for go_file in _go_files(pkg):
            for rel in _MODULE_IMPORT.findall(go_file.read_text(encoding="utf-8")):
                dep = COLLECTOR / rel
                if dep.is_dir() and dep not in seen:
                    queue.append(dep)
    return seen


def binary_env_vars(main_go: Path) -> set[str]:
    found: set[str] = set()
    for pkg in package_closure(main_go):
        files = _go_files(pkg)
        pkg_text = "\n".join(f.read_text(encoding="utf-8") for f in files)
        for go_file in files:
            found |= env_vars_in(go_file.read_text(encoding="utf-8"), pkg_text)
    return found


def section_of(page: str, binary: str) -> str:
    """The text of the page's ``## <binary>`` section, up to the next ``## `` heading."""
    match = re.search(rf"^## {re.escape(binary)}[ \t]*$(.*?)(?=^## |\Z)", page, re.M | re.S)
    assert match, f"docs/collector-reference.md has no '## {binary}' section"
    return match.group(1)


def flag_documented(section: str, name: str) -> bool:
    """True when the section names exactly ``--<name>`` in code (not ``--<name>-x``)."""
    return re.search(rf"`--{re.escape(name)}(?=[\s`=])", section) is not None


def _documented_binaries() -> list[Path]:
    return sorted(path for path in CMD_DIR.glob("*/main.go") if path.parent.name not in EXCLUDED)


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


def test_parser_finds_flags_and_env_vars_in_go_source() -> None:
    source = """
    spool := flag.String("spool", os.Getenv("NOVAFABRIC_SPOOL_DIR"), "dir")
    nats := flag.String("nats", envOr("NOVA_NATS_URL", "nats://x"), "url")
    every := flag.Duration("interval", time.Second, "d")
    fs := flag.NewFlagSet("verifier", flag.ContinueOnError)
    wal := fs.Bool("local-wal", false, "dev key")
    flag.IntVar(&n, "max-events", 10, "n")
    """
    assert flags_in(source) == {"spool", "nats", "interval", "local-wal", "max-events"}
    assert env_vars_in(source) == {"NOVAFABRIC_SPOOL_DIR", "NOVA_NATS_URL"}


def test_parser_ignores_calls_that_are_not_flag_definitions() -> None:
    source = (
        'slog.String("dir", d)\nslog.Int("n", 3)\nattr.Bool("ok", true)\n'
        'fmt.Println("spool")\nos.Setenv("NOT_READ", "1")'
    )
    assert flags_in(source) == set()
    assert env_vars_in(source) == set()


def test_env_parser_resolves_constants_and_home() -> None:
    source = "v := os.Getenv(envDLQDir)\nhome, _ := os.UserHomeDir()"
    package = source + '\nconst envDLQDir = "NOVA_DLQ_DIR"'
    assert env_vars_in(source, package) == {"NOVA_DLQ_DIR", "HOME"}


def test_flag_match_is_exact_and_scoped_to_its_section() -> None:
    page = (
        "## novafabric-a\n\n| `--spool-dir DIR` | x |\n\n## novafabric-b\n\n| `--spool DIR` | y |\n"
    )
    a, b = section_of(page, "novafabric-a"), section_of(page, "novafabric-b")
    assert not flag_documented(a, "spool"), "--spool-dir must not count as --spool"
    assert flag_documented(b, "spool")
    assert not flag_documented(a, "max")


# ---------------------------------------------------------------------------
# The guard itself
# ---------------------------------------------------------------------------


def test_scan_is_not_vacuous() -> None:
    """The scan must see the binaries, their flags and their env vars."""
    binaries = {path.parent.name: path for path in _documented_binaries()}
    assert {
        "novafabric-spool-forwarder",
        "novafabric-verifier",
        "novafabric-collector",
    } <= set(binaries)
    total = sum(len(flags_in(path.read_text(encoding="utf-8"))) for path in binaries.values())
    assert total >= 10, (
        f"only {total} flags found in {sorted(binaries)}: has the flag style changed?"
    )
    # Read outside main.go, so these prove the import closure is followed.
    assert "NOVA_DLQ_DIR" in binary_env_vars(binaries["novafabric-spool-forwarder"])
    assert {"NOVAFABRIC_ENV", "HOME"} <= binary_env_vars(binaries["novafabric-verifier"])


@pytest.mark.parametrize("main_go", _documented_binaries(), ids=lambda p: p.parent.name)
def test_every_flag_is_in_its_binary_section(main_go: Path) -> None:
    binary = main_go.parent.name
    section = section_of(REFERENCE.read_text(encoding="utf-8"), binary)
    missing = sorted(
        name
        for name in flags_in(main_go.read_text(encoding="utf-8"))
        if not flag_documented(section, name)
    )
    assert not missing, (
        f"{binary} defines flag(s) {missing} that the '## {binary}' section of "
        "docs/collector-reference.md does not document as `--<name>`."
    )


@pytest.mark.parametrize("main_go", _documented_binaries(), ids=lambda p: p.parent.name)
def test_every_env_var_is_in_its_binary_section(main_go: Path) -> None:
    binary = main_go.parent.name
    section = section_of(REFERENCE.read_text(encoding="utf-8"), binary)
    missing = sorted(name for name in binary_env_vars(main_go) if f"`{name}" not in section)
    assert not missing, (
        f"{binary} (or a collector package it imports) reads environment variable(s) "
        f"{missing} that the '## {binary}' section of docs/collector-reference.md does not name."
    )


def test_reference_page_is_linked_from_the_docs_index() -> None:
    index = (REPO_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
    assert "(collector-reference.md)" in index
