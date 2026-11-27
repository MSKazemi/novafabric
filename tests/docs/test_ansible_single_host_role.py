"""The single-host Ansible role in ``deploy/ansible/`` keeps its safety properties (#235).

CI lints the role and syntax-checks its playbooks (``.github/workflows/ansible.yml``),
but neither tool knows what the role is for. These checks pin what a reviewer would
otherwise have to re-read on every change:

* secrets and the package version have no defaults, the secrets file is 0600 and
  every task that receives the secret environment hides it (``no_log``);
* the systemd unit stays hardened and the data directory is its only writable path;
* migrations run as a handler, on the registry track only (the MetadataStore track
  shares Postgres's ``alembic_version`` table and makes the server refuse to start);
* nothing in the role removes the data or backup directories;
* every ``nova`` command the role runs resolves against the real CLI, flags included,
  so a renamed flag fails here instead of on an operator's host;
* the CI job exists, runs both tools on every playbook, and is pinned to an image.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.cli.introspect import root_command, subcommands

REPO = Path(__file__).resolve().parents[2]
ANSIBLE = REPO / "deploy" / "ansible"
ROLE = ANSIBLE / "roles" / "novafabric"
WORKFLOW = REPO / ".github" / "workflows" / "ansible.yml"

PLAYBOOKS = ("site.yml", "restore.yml", "uninstall.yml")
SECRET_VARS = ("novafabric_postgres_dsn", "novafabric_server_token", "novafabric_scim_token")
NOVA_BIN = "{{ novafabric_nova_bin }}"

_JINJA_QUOTED = re.compile(r"'([^'{}]+)'")
_FLAG = re.compile(r"(?<![\w/.-])(--?[A-Za-z][\w-]*)")


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _task_files() -> list[Path]:
    return sorted((ROLE / "tasks").glob("*.yml")) + [ROLE / "handlers" / "main.yml"]


def _walk_tasks(tasks: list[dict[str, Any]] | None) -> Iterator[dict[str, Any]]:
    for task in tasks or []:
        yield task
        for key in ("block", "rescue", "always"):
            yield from _walk_tasks(task.get(key))


def _all_tasks() -> Iterator[tuple[str, dict[str, Any]]]:
    for path in _task_files():
        for task in _walk_tasks(_load(path)):
            yield path.relative_to(ROLE).as_posix(), task


def _command_args(task: Mapping[str, Any]) -> list[str] | None:
    """The tokens of a command task that runs ``nova``, or ``None``."""
    module = task.get("ansible.builtin.command")
    if not isinstance(module, Mapping):
        return None
    argv = module.get("argv")
    if isinstance(argv, list):
        tokens = [str(t) for t in argv]
        return tokens[1:] if tokens and tokens[0] == NOVA_BIN else None
    if isinstance(argv, str) and "novafabric_nova_bin" in argv:
        # A templated argv list: its literal words are the quoted strings.
        return _JINJA_QUOTED.findall(argv)
    return None


def _exec_start_lines() -> Iterator[tuple[str, str]]:
    for template in sorted((ROLE / "templates").glob("*.service.j2")):
        for line in template.read_text(encoding="utf-8").splitlines():
            if line.startswith("ExecStart="):
                yield template.name, line.removeprefix("ExecStart=")


def _nova_invocations() -> list[tuple[str, list[str], list[str]]]:
    """(where, sub-command words, flags) for every nova command the role runs."""
    found = []
    for where, task in _all_tasks():
        tokens = _command_args(task)
        if tokens is None:
            continue
        words = []
        for token in tokens:
            if token.startswith("-") or "{{" in token:
                break
            words.append(token)
        flags = [t for t in tokens if t.startswith("-")]
        found.append((f"{where}: {task.get('name')}", words, flags))
    for where, line in _exec_start_lines():
        assert line.startswith(NOVA_BIN), f"{where}: ExecStart must run the venv's nova"
        rest = line.removeprefix(NOVA_BIN).split()
        words = []
        for token in rest:
            if token.startswith("-") or "{{" in token:
                break
            words.append(token)
        found.append((f"{where}: ExecStart", words, _FLAG.findall(line)))
    return found


def _resolve(words: list[str]) -> Any:
    command = root_command()
    for word in words:
        children = subcommands(command)
        assert children is not None and word in children, f"nova {' '.join(words)}: no {word!r}"
        command = children[word]
    return command


def _option_names(command: Any) -> set[str]:
    names = {"--help"}
    for param in getattr(command, "params", []):
        names.update(getattr(param, "opts", []))
        names.update(getattr(param, "secondary_opts", []))
    return names


# --------------------------------------------------------------------------- structure


@pytest.mark.parametrize(
    "rel",
    [
        "README.md",
        "ansible.cfg",
        ".ansible-lint",
        "inventory.example.yml",
        *PLAYBOOKS,
        "roles/novafabric/tasks/main.yml",
        "roles/novafabric/tasks/restore.yml",
        "roles/novafabric/tasks/uninstall.yml",
        "roles/novafabric/handlers/main.yml",
        "roles/novafabric/defaults/main.yml",
        "roles/novafabric/meta/main.yml",
        "roles/novafabric/templates/novafabric.service.j2",
        "roles/novafabric/templates/secrets.env.j2",
    ],
)
def test_role_file_exists(rel: str) -> None:
    assert (ANSIBLE / rel).is_file(), f"deploy/ansible/{rel} is missing"


def test_every_playbook_targets_the_role() -> None:
    for name in PLAYBOOKS:
        text = (ANSIBLE / name).read_text(encoding="utf-8")
        assert "hosts: novafabric_servers" in text, name
        assert "novafabric" in text, name


def test_the_hpc_playbook_stays_separate() -> None:
    assert (REPO / "deploy" / "hpc" / "ansible" / "install.yml").is_file()
    assert "deploy/hpc" not in (ROLE / "tasks" / "main.yml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- secrets


def test_secrets_and_version_have_no_defaults() -> None:
    defaults = _load(ROLE / "defaults" / "main.yml")
    for var in (*SECRET_VARS, "novafabric_version"):
        assert var not in defaults, f"{var} must come from the inventory or vault, never a default"


def test_the_version_is_required_and_pinned_exactly() -> None:
    validate = (ROLE / "tasks" / "validate.yml").read_text(encoding="utf-8")
    assert "novafabric_version is defined" in validate
    variables = _load(ROLE / "vars" / "main.yml")
    assert "'=='" in variables["novafabric_requirement"]


def test_the_secrets_file_is_root_only() -> None:
    config = _load(ROLE / "tasks" / "config.yml")
    by_src = {
        t["ansible.builtin.template"]["src"]: t for t in config if "ansible.builtin.template" in t
    }
    secrets = by_src["secrets.env.j2"]
    assert secrets["ansible.builtin.template"]["mode"] == "0600"
    assert secrets["ansible.builtin.template"]["owner"] == "root"
    assert "no_log" in secrets
    assert by_src["server.yaml.j2"]["ansible.builtin.template"]["mode"] == "0640"


def test_every_task_given_the_secret_environment_hides_it() -> None:
    offenders = [
        f"{where}: {task.get('name')}"
        for where, task in _all_tasks()
        if "novafabric_secret_env" in str(task.get("environment", "")) and "no_log" not in task
    ]
    assert not offenders, offenders


def test_secrets_never_reach_a_command_line() -> None:
    for where, task in _all_tasks():
        module = task.get("ansible.builtin.command")
        if isinstance(module, Mapping):
            argv = str(module.get("argv", ""))
            for var in SECRET_VARS:
                assert var not in argv, f"{where}: {task.get('name')} passes {var} on argv"
    for where, line in _exec_start_lines():
        assert "dsn" not in line.lower() and "token" not in line.lower(), where


# --------------------------------------------------------------------------- systemd unit


def test_the_unit_is_hardened_and_writes_only_the_data_dir() -> None:
    unit = (ROLE / "templates" / "novafabric.service.j2").read_text(encoding="utf-8")
    for directive in (
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "User={{ novafabric_user }}",
    ):
        assert directive in unit, directive
    writable = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert writable == ["ReadWritePaths={{ novafabric_data_dir }}"]
    assert "--insecure-no-auth" not in unit


# --------------------------------------------------------------------------- migrations


def test_migrations_run_from_a_handler_on_the_registry_track_only() -> None:
    upgrades = [
        (where, tokens)
        for where, task in _all_tasks()
        if (tokens := _command_args(task)) and tokens[:2] == ["db", "upgrade"]
    ]
    assert any(where == "handlers/main.yml" for where, _ in upgrades), "no migration handler"
    for where, tokens in upgrades:
        allowed = {"handlers/main.yml", "tasks/restore.yml"}
        assert where in allowed, f"db upgrade outside the handler: {where}"
        assert tokens[tokens.index("--track") + 1] == "registry", where
    install = (ROLE / "tasks" / "install.yml").read_text(encoding="utf-8")
    assert "Run the NovaFabric registry migrations" in install


def test_migrations_are_flushed_before_the_service_starts() -> None:
    service = _load(ROLE / "tasks" / "service.yml")
    names = [next(iter(k for k in t if k.startswith("ansible."))) for t in service]
    flush = names.index("ansible.builtin.meta")
    start = next(i for i, t in enumerate(service) if t.get("ansible.builtin.systemd_service"))
    assert flush < start


# --------------------------------------------------------------------------- data safety


def test_nothing_removes_the_data_or_backup_directories() -> None:
    protected = (
        "novafabric_data_dir",
        "novafabric_home",
        "novafabric_backup_dir",
        "novafabric_audit",
    )
    for where, task in _all_tasks():
        module = task.get("ansible.builtin.file")
        if isinstance(module, Mapping) and module.get("state") == "absent":
            path = str(module.get("path", ""))
            assert not any(p in path for p in protected), f"{where}: removes {path}"
        assert "ansible.builtin.shell" not in task, f"{where}: use command/argv, not shell"


def test_restore_verifies_the_set_before_restoring() -> None:
    tasks = list(_walk_tasks(_load(ROLE / "tasks" / "restore.yml")))
    order = [
        " ".join(tokens[:2]) if tokens[0] == "backup" else tokens[0]
        for t in tasks
        if (tokens := _command_args(t))
    ]
    assert order.index("backup verify") < order.index("restore"), order
    restore = next(t for t in tasks if (tokens := _command_args(t)) and tokens[:1] == ["restore"])
    tokens = _command_args(restore) or []
    assert "--home" in tokens
    assert "--force" in tokens and "--restore-keys" in tokens  # both opt-in, from variables
    defaults = _load(ROLE / "defaults" / "main.yml")
    assert defaults["novafabric_restore_force"] is False
    assert defaults["novafabric_restore_keys"] is False


# --------------------------------------------------------------------------- real CLI


def test_the_role_runs_nova_commands() -> None:
    found = _nova_invocations()
    words = {" ".join(w) for _, w, _ in found}
    assert {"server start", "db upgrade", "backup create", "backup verify", "restore"} <= words


@pytest.mark.parametrize(
    ("where", "words", "flags"), _nova_invocations(), ids=lambda v: str(v)[:60]
)
def test_every_nova_command_resolves_against_the_cli(
    where: str, words: list[str], flags: list[str]
) -> None:
    command = _resolve(words)
    unknown = sorted(set(flags) - _option_names(command))
    assert not unknown, f"{where}: nova {' '.join(words)} has no {unknown}"


# --------------------------------------------------------------------------- CI


def test_the_ci_job_lints_and_syntax_checks_every_playbook() -> None:
    doc = _load(WORKFLOW)
    (job,) = doc["jobs"].values()
    assert job["runs-on"] == "ubuntu-24.04"
    run = "\n".join(step.get("run", "") for step in job["steps"])
    assert "ansible-lint" in run and "--syntax-check" in run
    for playbook in PLAYBOOKS:
        assert playbook in run, playbook
