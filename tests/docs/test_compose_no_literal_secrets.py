"""Guard: no Compose file ships a literal password, secret or token.

Before this guard, ``deploy/docker/docker-compose.yml`` set
``POSTGRES_PASSWORD: nova``, ``CLICKHOUSE_PASSWORD: novapass`` and DSNs such as
``postgresql://nova:nova@postgres:5432/nova``. A first deployment that never
edited the file kept those demo credentials for good: nothing failed, nothing
warned, and the Postgres volume stored the demo password permanently.

The rule now: every credential in a Compose file is a **required** variable,
``${VAR:?message}``, so ``docker compose`` stops with that message until the
operator (or ``scripts/compose_bootstrap.py``) sets it. A default
(``${VAR:-nova}``) is a literal secret with extra steps and fails here too.

What is checked, without Docker (the YAML is parsed directly):

1. every ``environment`` entry whose name looks secret-bearing (``PASSWORD``,
   ``PASSWD``, ``SECRET``, ``TOKEN``, ``API_KEY``, ``PRIVATE_KEY``,
   ``CREDENTIAL``) is unset or a ``${VAR:?...}`` reference;
2. every URL/DSN with a ``user:password@`` part, anywhere in a service
   (environment, command, healthcheck), takes the password from a required
   variable;
3. no command or healthcheck passes ``--password <literal>``;
4. the bootstrap script generates exactly the secrets the compose file needs,
   and the generated file is excluded from git and from the build context.

Scope: every tracked ``docker-compose*.y*ml`` / ``compose*.y*ml`` outside
``tests/``. Test rigs under ``tests/`` are disposable fixtures with throwaway
credentials and are listed explicitly in :data:`DISPOSABLE_FIXTURES`, so a new
production-path compose file cannot hide among them.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_COMPOSE = REPO_ROOT / "deploy" / "docker" / "docker-compose.yml"
BOOTSTRAP = REPO_ROOT / "scripts" / "compose_bootstrap.py"

# Throwaway-credential test rigs. Each is started only by an opt-in test, binds
# a disposable database, and is never a deployment path.
DISPOSABLE_FIXTURES = frozenset({"tests/integration/docker-compose.eval.yaml"})

_COMPOSE_NAME_RE = re.compile(r"(^|/)(docker-)?compose[^/]*\.ya?ml$")
_SECRET_NAME_RE = re.compile(
    r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|CREDENTIAL", re.IGNORECASE
)
# A required reference, ${NAME:?message}. The message may contain spaces, so
# every reference is collapsed to one placeholder before a string is scanned.
_REQUIRED_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:\?[^}]*\}")
_PLACEHOLDER = "${REQUIRED}"
# scheme://user:password@host — password up to the last '@' before the host.
_USERINFO_RE = re.compile(r"[a-z][a-z0-9+.-]*://[^:/@\s]+:(?P<password>.+?)@[^@\s]*$", re.I)
_PASSWORD_FLAG_RE = re.compile(r"--password[=\s]+(?P<value>\"[^\"]*\"|'[^']*'|\S+)")
# Container-side shell expansion ($$VAR -> $VAR in the container) is not a literal.
_CONTAINER_VAR_RE = re.compile(r"^[\"']?\$\$\{?[A-Za-z_][A-Za-z0-9_]*\}?[\"']?$")


def _tracked_compose_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return sorted(p for p in out.splitlines() if _COMPOSE_NAME_RE.search(p))


def _production_compose_files() -> list[str]:
    return [p for p in _tracked_compose_files() if not p.startswith("tests/")]


def _environment_items(service: dict[str, Any]) -> Iterator[tuple[str, str]]:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        for key, value in env.items():
            yield str(key), "" if value is None else str(value)
    else:  # list form: ["KEY=value", "KEY"]
        for entry in env:
            key, _, value = str(entry).partition("=")
            yield key, value


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def _collapse(text: str) -> str:
    return _REQUIRED_REF_RE.sub(lambda _m: _PLACEHOLDER, text)


def _is_required_ref(value: str) -> bool:
    return _collapse(value.strip()) == _PLACEHOLDER


def literal_secret_problems(compose: dict[str, Any], label: str) -> list[str]:
    """Return one message per literal or defaulted credential in *compose*."""
    problems: list[str] = []
    for name, service in (compose.get("services") or {}).items():
        where = f"{label}: service {name!r}"
        for key, value in _environment_items(service or {}):
            if _SECRET_NAME_RE.search(key) and value and not _is_required_ref(value):
                problems.append(f"{where}: {key} is not a ${{VAR:?...}} reference")
        for text in map(_collapse, _strings(service or {})):
            for token in text.split():
                match = _USERINFO_RE.search(token)
                if match and match.group("password") != _PLACEHOLDER:
                    problems.append(f"{where}: URL carries a literal password: {token[:40]}...")
            for match in _PASSWORD_FLAG_RE.finditer(text):
                value = match.group("value")
                if not (_CONTAINER_VAR_RE.match(value) or value.strip("\"'") == _PLACEHOLDER):
                    problems.append(f"{where}: --password is given a literal value")
    return problems


def test_there_are_compose_files_to_check() -> None:
    """Guard the guard: an empty scan would pass vacuously."""
    files = _production_compose_files()
    assert "deploy/docker/docker-compose.yml" in files, files


def test_disposable_fixtures_still_exist_and_stay_under_tests() -> None:
    tracked = set(_tracked_compose_files())
    for fixture in DISPOSABLE_FIXTURES:
        assert fixture.startswith("tests/"), fixture
        assert fixture in tracked, f"stale DISPOSABLE_FIXTURES entry: {fixture}"


def test_no_compose_file_contains_a_literal_secret() -> None:
    problems: list[str] = []
    for rel in _production_compose_files():
        compose = yaml.safe_load((REPO_ROOT / rel).read_text(encoding="utf-8")) or {}
        problems += literal_secret_problems(compose, rel)
    assert not problems, (
        "Compose files must take every credential from a required variable "
        "(${VAR:?message}) — no literal values, no ${VAR:-default}:\n  " + "\n  ".join(problems)
    )


def test_known_demo_values_are_gone_from_the_main_compose_file() -> None:
    text = MAIN_COMPOSE.read_text(encoding="utf-8")
    for demo in ("nova:nova@", "novapass", "POSTGRES_PASSWORD: nova\n"):
        assert demo not in text, f"demo credential {demo!r} is back in {MAIN_COMPOSE}"


# ── Negative controls: the checker must catch what it claims to catch ────────


def _synthetic(environment: Any = None, **extra: Any) -> dict[str, Any]:
    service: dict[str, Any] = {"image": "postgres:16-alpine", **extra}
    if environment is not None:
        service["environment"] = environment
    return {"services": {"db": service}}


def test_checker_flags_a_literal_password() -> None:
    assert literal_secret_problems(_synthetic({"POSTGRES_PASSWORD": "nova"}), "x")


def test_checker_flags_a_defaulted_password() -> None:
    compose = _synthetic({"POSTGRES_PASSWORD": "${NOVA_POSTGRES_PASSWORD:-nova}"})
    assert literal_secret_problems(compose, "x")


def test_checker_flags_list_form_environment() -> None:
    assert literal_secret_problems(_synthetic(["API_TOKEN=abc123"]), "x")


def test_checker_flags_a_literal_password_in_a_dsn() -> None:
    compose = _synthetic({"DSN": "postgresql://nova:nova@postgres:5432/nova"})
    assert literal_secret_problems(compose, "x")


def test_checker_flags_a_literal_password_flag_in_a_healthcheck() -> None:
    compose = _synthetic(
        healthcheck={"test": ["CMD-SHELL", "clickhouse-client --password novapass -q 1"]}
    )
    assert literal_secret_problems(compose, "x")


def test_checker_accepts_required_references() -> None:
    ref = "${NOVA_POSTGRES_PASSWORD:?run scripts/compose_bootstrap.py first}"
    compose = _synthetic(
        {"POSTGRES_PASSWORD": ref, "DSN": f"postgresql://nova:{ref}@postgres:5432/nova"},
        healthcheck={"test": ["CMD-SHELL", 'clickhouse-client --password "$$CH_PW" -q 1']},
    )
    assert literal_secret_problems(compose, "x") == []


# ── The bootstrap script and the compose file agree ──────────────────────────


def _bootstrap_required_secrets() -> set[str]:
    tree = ast.parse(BOOTSTRAP.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign | ast.Assign):
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if any(isinstance(t, ast.Name) and t.id == "REQUIRED_SECRETS" for t in targets):
                assert node.value is not None
                return set(ast.literal_eval(node.value))
    raise AssertionError("REQUIRED_SECRETS not found in scripts/compose_bootstrap.py")


def test_bootstrap_generates_exactly_the_secrets_the_compose_file_requires() -> None:
    text = MAIN_COMPOSE.read_text(encoding="utf-8")
    required = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):\?", text))
    secret_vars = {name for name in required if _SECRET_NAME_RE.search(name)}
    assert secret_vars, "the compose file declares no required secrets"
    assert secret_vars == _bootstrap_required_secrets(), (
        "scripts/compose_bootstrap.py REQUIRED_SECRETS and the ${VAR:?} secrets in "
        f"{MAIN_COMPOSE.name} differ: compose={sorted(secret_vars)} "
        f"bootstrap={sorted(_bootstrap_required_secrets())}"
    )


def test_generated_env_file_is_ignored_by_git() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", "deploy/docker/.env"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "deploy/docker/.env must be git-ignored"


def test_generated_env_file_is_excluded_from_the_docker_build_context() -> None:
    patterns = {
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "**/.env" in patterns, ".dockerignore must exclude **/.env (compose secrets)"
