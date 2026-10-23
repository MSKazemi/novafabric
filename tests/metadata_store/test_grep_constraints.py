"""FR-07 / FR-13: static guards on the RLS primitives, in pure Python.

History, kept because each step was a measured correction:

* These guards used to shell out to ``grep -E`` and assert only on its exit
  code. ``grep -E`` has no lookahead, so the FR-07 pattern ``(?!LOCAL…)`` was a
  syntax error and the test passed even on a known-bad line; the FR-13 guard,
  run from any directory but the repo root, found no ``src/`` and passed too.
* The regex rewrite that followed recognised *bad* forms, line by line. An
  adversarial review (2026-10-22, ``a2_rls_guard_bypass.py``) wrote eleven
  session-scoped tenant-GUC writes that Postgres accepts and the guard missed —
  string concatenation, f-strings, SQL split across lines, a quoted identifier,
  ``set_config`` with the name or ``false`` passed as a parameter, ``'false'``,
  ``0::bool``, psycopg ``sql.SQL`` composition and libpq ``options=-c`` — and
  four FR-13 primitives it missed by case or whitespace.

A static scan cannot recognise every bad form, so it no longer tries. It is
**default-deny**: every executable occurrence of a tenant-GUC or RLS primitive
(case-insensitive, whitespace- and newline-tolerant, across string
concatenation and f-strings, in string literals *and* identifiers) is a
finding unless it is one of the explicitly allowlisted, known-good call sites in
``metadata_store/``. A new call site — good or bad — fails this test until a
reviewer adds it to :data:`KNOWN_GOOD_SITES`.

The scan is the cheap first line. The behavioural guard in
``test_tenant_guc_behaviour.py`` drives the real store and checks every
statement it executes, which no spelling can evade.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "novafabric"
METADATA_STORE = SRC_ROOT / "metadata_store"

#: Known-bad fixture: a store that sets the tenant GUC with a session-scoped SET.
BROKEN_SESSION_SET = REPO_ROOT / "tests" / "fixtures" / "broken_session_set.py"

# ── what counts as a primitive ──────────────────────────────────────────────

#: Anything that can write the tenant GUC: its name, the function spelling, and
#: the libpq connection option (``options='-c name=value'`` / ``PGOPTIONS``).
#: Matched against a *normalised* site (lower case, whitespace collapsed).
_TENANT_WRITE = re.compile(
    r"current_tenant_id|set_config|pgoptions|(?:^|[\s'\"=?&])-c\s*\S+\s*="
)
#: FR-13 adds the read side and the table switch: none of these may appear
#: outside ``metadata_store/``.
_RLS_PRIMITIVE = re.compile(
    _TENANT_WRITE.pattern + r"|current_setting|force row level|row level security"
)

#: The policy's read of the tenant GUC — a read, not a write.
_SAFE_READ = re.compile(r"current_setting\(\s*'app\.current_tenant_id'(?:::text)?\s*\)")
#: The only transaction-scoped write forms (FR-07). Full-match, after
#: normalisation; ``{}`` stands for an f-string / ``.format`` hole.
_SAFE_WRITES = (
    re.compile(r"set local app\.current_tenant_id (?:=|to) '[^']*'"),
    re.compile(r"select set_config\('app\.current_tenant_id', (?:%s|'[^']*'), true\)"),
)


def normalise(text: str) -> str:
    """Lower case, whitespace (including newlines) collapsed to one space."""
    return " ".join(text.split()).lower()


@dataclass(frozen=True)
class Site:
    """One executable occurrence of a primitive in a source file."""

    path: Path
    lineno: int
    text: str  # normalised

    def render(self, root: Path | None = None) -> str:
        shown = self.path.relative_to(root).as_posix() if root else str(self.path)
        return f"{shown}:{self.lineno}: {self.text}"


# ── extraction: string templates and identifiers, never comments/docstrings ─


def _template(node: ast.AST) -> str | None:
    """The string *node* builds, with ``{}`` for every hole; None if not a string.

    Joins concatenation and f-strings, so a name split across ``+`` or built in
    an f-string is seen whole. ``"..." % x`` and ``"...".format(x)`` keep their
    receiver's template (the placeholders are already in it).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else "{}"
            for v in node.values
        )
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _template(node.left), _template(node.right)
        if left is None and right is None:
            return None
        return (left if left is not None else "{}") + (right if right is not None else "{}")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        return _template(node.left)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
    ):
        return _template(node.func.value)
    return None


def _children_to_visit(node: ast.AST) -> Iterator[ast.AST]:
    """Sub-expressions of a templated node that are not part of its template."""
    if isinstance(node, ast.JoinedStr):
        for v in node.values:
            if isinstance(v, ast.FormattedValue):
                yield v.value
    elif isinstance(node, ast.BinOp):
        for side in (node.left, node.right):
            if _template(side) is None:
                yield side
            else:
                yield from _children_to_visit(side)
    elif isinstance(node, ast.Call):
        yield from node.args
        yield from (k.value for k in node.keywords)
        if isinstance(node.func, ast.Attribute):
            yield from _children_to_visit(node.func.value)


_IDENTIFIER = re.compile(r"set_config|current_setting|current_tenant_id|pgoptions", re.I)


def _sites_in_tree(path: Path, tree: ast.AST) -> Iterator[Site]:
    stack: list[ast.AST] = [tree]
    while stack:
        node = stack.pop()
        # A bare string statement (a docstring) executes nothing.
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        template = _template(node)
        if template is not None:
            yield Site(path, getattr(node, "lineno", 0), normalise(template))
            stack.extend(_children_to_visit(node))
            continue
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.keyword):
            name = node.arg
            if node.arg == "options":  # libpq: options="-c name=value"
                value = _template(node.value)
                if value is not None and "-c" in value:
                    yield Site(path, node.value.lineno, normalise(f"options={value}"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
        if name and _IDENTIFIER.search(name):
            yield Site(path, getattr(node, "lineno", 0), normalise(name))
        stack.extend(ast.iter_child_nodes(node))


def primitive_sites(path: Path) -> list[Site]:
    """Every executable occurrence of an RLS / tenant-GUC primitive in *path*.

    A file that does not parse is scanned as raw text, line by line: an
    unparseable file must not be a way to hide a primitive.
    """
    text = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        found = [Site(path, n, normalise(line)) for n, line in enumerate(text.splitlines(), 1)]
    else:
        found = list(_sites_in_tree(path, tree))
    hits = [s for s in found if _RLS_PRIMITIVE.search(s.text)]
    return sorted(hits, key=lambda s: (s.lineno, s.text))


def is_transaction_scoped(site: Site) -> bool:
    """FR-07: True if *site* writes the tenant GUC only transaction-scoped.

    Default-deny: a site that mentions a tenant-GUC write primitive passes only
    if it is exactly one of :data:`_SAFE_WRITES`, or if nothing write-capable is
    left once the policy's own reads are removed.
    """
    if any(form.fullmatch(site.text) for form in _SAFE_WRITES):
        return True
    return not _TENANT_WRITE.search(_SAFE_READ.sub("", site.text))


def find_session_sets(paths: Iterable[Path]) -> list[str]:
    """FR-07: every tenant-GUC write in *paths* not provably transaction-scoped."""
    return [
        s.render()
        for path in paths
        for s in primitive_sites(path)
        if not is_transaction_scoped(s)
    ]


_RLS_ALLOWED = (
    "src/novafabric/metadata_store/",
    "tests/metadata_store/",
    "tests/fixtures/broken_session_set.py",
)


def _python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def find_rls_primitives_outside(root: Path, scan_dirs: Iterable[str]) -> list[str]:
    """FR-13: every primitive in *scan_dirs* outside the allowed paths."""
    violations: list[str] = []
    for scan_dir in scan_dirs:
        for path in _python_files(root / scan_dir):
            if path.relative_to(root).as_posix().startswith(_RLS_ALLOWED):
                continue
            violations.extend(s.render(root) for s in primitive_sites(path))
    return violations


# ── the explicit allowlist of known-good call sites ─────────────────────────


@dataclass(frozen=True)
class KnownGood:
    """One reviewed call site: a file and the exact (normalised) form it uses."""

    path: str  # relative to src/novafabric/metadata_store/
    form: re.Pattern[str]
    why: str


_POLICY_DDL = re.compile(
    r"(?:create policy tenant_isolation on \{\} )?using \( ?tenant_id = "
    r"current_setting\('app\.current_tenant_id'\)::uuid ?\) with check \( ?tenant_id = "
    r"current_setting\('app\.current_tenant_id'\)::uuid ?\)"
)
_FORCE_RLS = re.compile(r"alter table \{\} (?:enable|force) row level security")

#: Every primitive in ``metadata_store/`` must match one of these — so a new
#: call site, even a correct one, fails until a reviewer adds it here.
KNOWN_GOOD_SITES: tuple[KnownGood, ...] = (
    KnownGood("postgres.py",
              re.compile(r"set local app\.current_tenant_id = '\{\}'"),
              "begin_tenant_context: the one tenant-GUC write (FR-07)"),
    KnownGood("postgres.py", re.compile(r"\s*" + _POLICY_DDL.pattern + r"\s*"),
              "bootstrap(): the tenant_isolation policy reads the GUC"),
    KnownGood("postgres.py", _FORCE_RLS, "bootstrap(): ENABLE/FORCE RLS per table"),
    KnownGood("rls.py",
              re.compile(r"\(tenant_id = \(current_setting\('app\.current_tenant_id'::text\)\)"
                         r"::uuid\)"),
              "CANONICAL_POLICY_QUAL: the expected policy text, compared, never executed"),
    KnownGood("migrations/postgres/versions/v001_init.py", _FORCE_RLS, "v001: RLS on"),
    KnownGood("migrations/postgres/versions/v001_init.py",
              re.compile(r"\s*" + _POLICY_DDL.pattern + r"\s*"), "v001: policy"),
    KnownGood("migrations/postgres/versions/v002_partition_ddl.py", _FORCE_RLS,
              "v002: RLS on the partitioned parents"),
    KnownGood("migrations/postgres/versions/v002_partition_ddl.py",
              re.compile(r"\s*" + _POLICY_DDL.pattern + r"\s*"), "v002: policy"),
    KnownGood("migrations/postgres/versions/v004_runs_tenant_in_pk.py", _FORCE_RLS,
              "v004: RLS on the re-keyed runs table"),
    KnownGood("migrations/postgres/versions/v004_runs_tenant_in_pk.py",
              re.compile(r"\s*" + _POLICY_DDL.pattern + r"\s*"), "v004: policy"),
)


def unreviewed_sites(store_root: Path) -> list[str]:
    """Primitives in *store_root* that match no :data:`KNOWN_GOOD_SITES` entry."""
    out: list[str] = []
    for path in _python_files(store_root):
        rel = path.relative_to(store_root).as_posix()
        forms = [k.form for k in KNOWN_GOOD_SITES if k.path == rel]
        for site in primitive_sites(path):
            if not any(form.fullmatch(site.text) for form in forms):
                out.append(site.render(store_root))
    return out


# ── FR-07: no session-scoped write of the tenant GUC ────────────────────────


def test_no_session_set_in_src() -> None:
    """FR-07: no tenant-GUC write outside the transaction-scoped forms, in all of src/."""
    files = _python_files(SRC_ROOT)
    # Non-vacuous: the scan really covers the file that sets the tenant GUC.
    assert METADATA_STORE / "postgres.py" in files
    hits = find_session_sets(files)
    assert not hits, (
        "Tenant GUC written in a form not provably transaction-scoped — use "
        "SET LOCAL inside begin_tenant_context():\n" + "\n".join(hits)
    )


def test_every_primitive_in_metadata_store_is_a_reviewed_call_site() -> None:
    """Default-deny: a new call site fails until it is added to KNOWN_GOOD_SITES."""
    unknown = unreviewed_sites(METADATA_STORE)
    assert not unknown, (
        "Unreviewed RLS / tenant-GUC primitive in metadata_store/ — review it and "
        "add it to KNOWN_GOOD_SITES:\n" + "\n".join(unknown)
    )


@pytest.mark.parametrize("entry", KNOWN_GOOD_SITES, ids=lambda k: f"{k.path}:{k.why}")
def test_every_allowlist_entry_is_still_used(entry: KnownGood) -> None:
    """Non-vacuous and not stale: each allowlisted call site really exists."""
    sites = primitive_sites(METADATA_STORE / entry.path)
    assert any(entry.form.fullmatch(s.text) for s in sites), (
        f"KNOWN_GOOD_SITES entry no longer matches anything: {entry}"
    )


def test_real_store_uses_set_local() -> None:
    """Positive control: the shipped store sets the GUC with SET LOCAL, and it passes."""
    postgres = METADATA_STORE / "postgres.py"
    texts = [s.text for s in primitive_sites(postgres)]
    assert "set local app.current_tenant_id = '{}'" in texts
    assert find_session_sets([postgres]) == []


def test_guard_fires_on_known_bad_fixture() -> None:
    """Negative control: the session-SET mutant fixture is flagged on its SET line."""
    hits = find_session_sets([BROKEN_SESSION_SET])
    assert len(hits) == 1, hits
    assert "set app.current_tenant_id" in hits[0]


#: Session-scoped tenant-GUC writes Postgres accepts. The first six are the
#: original controls; the rest are the adversarial review's bypasses
#: (``a2_rls_guard_bypass.py``), each of which the previous guard missed.
SESSION_SCOPED_FORMS = {
    "plain": "conn.execute(\"SET app.current_tenant_id = 'x'\")",
    "lower": "conn.execute(\"set app.current_tenant_id = 'x'\")",
    "session": "conn.execute(\"SET SESSION app.current_tenant_id = 'x'\")",
    "tab_to": "conn.execute(\"SET\tapp.current_tenant_id TO 'x'\")",
    "set_config_false": "conn.execute(\"SELECT set_config('app.current_tenant_id', %s, false)\")",
    "set_config_upper": "conn.execute(\"SELECT SET_CONFIG('app.current_tenant_id', %s, FALSE)\")",
    "concat": 'conn.execute("SET " + "app.current_tenant_id = %s" % t)',
    "split_name": 'conn.execute("SET app." + "current_tenant_id = %s" % t)',
    "fstring_name": 'g = "app.current_tenant_id"\nconn.execute(f"SET {g} = \'{t}\'")',
    "multiline_triple": 'conn.execute("""\n    SET\n    app.current_tenant_id = \'x\'""")',
    "quoted_ident": "conn.execute('SET \"app.current_tenant_id\" = \\'x\\'')",
    "set_config_param_name":
        'conn.execute("SELECT set_config(%s, %s, false)", ("app.current_tenant_id", t))',
    "set_config_param_islocal":
        "conn.execute(\"SELECT set_config('app.current_tenant_id', %s, %s)\", (t, False))",
    "set_config_str_false":
        "conn.execute(\"SELECT set_config('app.current_tenant_id', %s, 'false')\")",
    "set_config_0bool":
        "conn.execute(\"SELECT set_config('app.current_tenant_id', %s, 0::bool)\")",
    "set_config_multiline":
        'conn.execute("""SELECT set_config(\n \'app.current_tenant_id\', %s,\n false)""")',
    "psycopg_sql_compose":
        'conn.execute(sql.SQL("SET {} = {}").format('
        'sql.Identifier("app", "current_tenant_id"), sql.Literal(t)))',
    "libpq_options": 'psycopg.connect(dsn, options="-c app.current_tenant_id=" + t)',
    "libpq_options_fstring": 'psycopg.connect(dsn, options=f"-c {name}={t}")',
    "pgoptions_env": 'os.environ["PGOPTIONS"] = opts',
    "sqlalchemy_func": "session.execute(select(func.set_config(name, t, False)))",
}


@pytest.mark.parametrize("code", SESSION_SCOPED_FORMS.values(), ids=SESSION_SCOPED_FORMS.keys())
def test_guard_flags_session_scoped_forms(tmp_path: Path, code: str) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(code + "\n", encoding="utf-8")
    assert find_session_sets([bad]), f"FR-07 guard missed: {code!r}"


@pytest.mark.parametrize(
    "line",
    [
        "conn.execute(\"SET LOCAL app.current_tenant_id = 'x'\")",
        "conn.execute(\"set local app.current_tenant_id = 'x'\")",
        "conn.execute(f\"SET LOCAL app.current_tenant_id = '{tid}'\")",
        "conn.execute(\"SELECT set_config('app.current_tenant_id', %s, true)\")",
        "conn.execute(\"SET statement_timeout = 0\")",
        "# the GUC must be set with SET LOCAL app.current_tenant_id = x",
        '"""Docstring: never SET app.current_tenant_id session-wide."""',
        "subprocess.run([sys.executable, '-m', 'alembic', '-c', ini])",
    ],
)
def test_guard_allows_transaction_scoped_forms(tmp_path: Path, line: str) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text(line + "\n", encoding="utf-8")
    assert find_session_sets([ok]) == []


def test_an_unparseable_file_is_scanned_as_text(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def (:\n    conn.execute(\"SET app.current_tenant_id = 'x'\")\n")
    assert len(find_session_sets([bad])) == 1


# ── FR-13: RLS primitives confined to metadata_store/ ───────────────────────


def test_rls_strings_confined_to_metadata_store() -> None:
    """FR-13: no RLS / tenant-GUC primitive outside metadata_store/."""
    violations = find_rls_primitives_outside(REPO_ROOT, ("src", "tests"))
    assert not violations, "RLS primitives found outside allowed paths:\n" + "\n".join(violations)


def test_rls_guard_sees_the_real_primitives() -> None:
    """Non-vacuous: the scan finds the shipped primitives inside metadata_store/."""
    texts = " ".join(s.text for s in primitive_sites(METADATA_STORE / "postgres.py"))
    assert "force row level security" in texts
    assert "current_setting('app.current_tenant_id')" in texts


def test_rls_guard_fires_outside_allowed_paths(tmp_path: Path) -> None:
    """Negative control: a primitive planted outside metadata_store/ is reported."""
    planted = tmp_path / "src" / "novafabric" / "server" / "leak.py"
    planted.parent.mkdir(parents=True)
    planted.write_text('op.execute("ALTER TABLE runs FORCE ROW LEVEL SECURITY")\n', encoding="utf-8")
    allowed = tmp_path / "src" / "novafabric" / "metadata_store" / "ok.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("x = \"current_setting('app.current_tenant_id')\"\n", encoding="utf-8")
    violations = find_rls_primitives_outside(tmp_path, ("src",))
    assert violations == [
        "src/novafabric/server/leak.py:1: alter table runs force row level security"
    ]


FR13_PRIMITIVES = {
    "upper_current_setting": 'q = "tenant_id = CURRENT_SETTING(\'app.current_tenant_id\')::uuid"',
    "space_paren": 'q = "tenant_id = current_setting (\'app.current_tenant_id\')::uuid"',
    "lower_force_rls": 'op.execute("alter table runs force row level security")',
    "multiline_force": 'op.execute("""ALTER TABLE runs FORCE\nROW LEVEL SECURITY""")',
    "concat_force": 'op.execute("ALTER TABLE runs FORCE ROW " + "LEVEL SECURITY")',
    "control": 'op.execute("ALTER TABLE runs FORCE ROW LEVEL SECURITY")',
    "enable_rls": 'op.execute("ALTER TABLE runs ENABLE ROW LEVEL SECURITY")',
    "bare_guc_name": 'GUC = "app.current_tenant_id"',
}


@pytest.mark.parametrize("code", FR13_PRIMITIVES.values(), ids=FR13_PRIMITIVES.keys())
def test_rls_guard_flags_every_spelling_outside(tmp_path: Path, code: str) -> None:
    planted = tmp_path / "src" / "novafabric" / "server" / "x.py"
    planted.parent.mkdir(parents=True)
    planted.write_text(code + "\n", encoding="utf-8")
    assert find_rls_primitives_outside(tmp_path, ("src",)), f"FR-13 guard missed: {code!r}"
