# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""``nova prompt`` — prompt versioning over the registry (ADR-0112, experimental).

Prompts are immutable, content-addressed registry versions: every edit is a
new version, and each version's ``content_hash`` makes the capsule reference
``prompt:<id>@<version>+sha256:<hex>`` verifiable. NovaFabric records prompt
evidence; it never renders templates or serves prompts to inference.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from novafabric.spec.models import AssetStatus
from novafabric.spec.prompt_asset import PromptTemplate, variable_mismatch_warnings

console = Console()

prompt_app = typer.Typer(
    name="prompt",
    help=(
        "Prompt versioning: register, get, list, history, diff, "
        "compose, tree (experimental, ADR-0112/0115)."
    ),
    no_args_is_help=True,
)


def _parse_ref(ref: str, require_version: bool = False) -> tuple[str, int | None]:
    """Split ``<prompt_id>[@<version>]`` into ``(prompt_id, version)``."""
    if "@" not in ref:
        if require_version:
            raise typer.BadParameter(
                f"Invalid prompt ref {ref!r}: expected <prompt_id>@<version>",
                param_hint="ref",
            )
        return ref, None
    prompt_id, _, raw_version = ref.rpartition("@")
    try:
        version = int(raw_version)
    except ValueError:
        raise typer.BadParameter(
            f"Invalid prompt version {raw_version!r}: expected an integer",
            param_hint="ref",
        ) from None
    return prompt_id, version


def _parse_selector_ref(ref: str) -> tuple[str, str | None]:
    """Split ``<prompt_id>[@<selector>]``; the selector stays a string.

    Unlike :func:`_parse_ref`, the selector may be an integer version *or*
    a deployment label (``@production``, ``@latest`` — ADR-0113/0115).
    """
    if "@" not in ref:
        return ref, None
    prompt_id, _, selector = ref.rpartition("@")
    if not prompt_id or not selector:
        raise typer.BadParameter(
            f"Invalid prompt ref {ref!r}: expected <prompt_id>[@<selector>]",
            param_hint="ref",
        )
    return prompt_id, selector


def _load_template(
    template: str | None, file: Path | None
) -> PromptTemplate:
    """Resolve the template body from ``--template`` or ``--file``.

    A ``.json`` file containing an array is a chat-form template
    (``[{role, content}, ...]``); any other file is a text-form template
    read verbatim.
    """
    if (template is None) == (file is None):
        raise typer.BadParameter(
            "Provide exactly one of --template or --file.",
            param_hint="--template / --file",
        )
    if template is not None:
        return template
    assert file is not None
    if not file.is_file():
        raise typer.BadParameter(f"File not found: {file}", param_hint="--file")
    text = file.read_text(encoding="utf-8")
    if file.suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(
                f"--file {file} is not valid JSON: {exc}", param_hint="--file"
            ) from None
        if not isinstance(data, list):
            raise typer.BadParameter(
                "A JSON template file must contain a chat-form array "
                "of {role, content} messages.",
                param_hint="--file",
            )
        return data
    return text


def _parse_config(config: str | None) -> dict[str, Any]:
    if config is None:
        return {}
    try:
        data = json.loads(config)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(
            f"--config is not valid JSON: {exc}", param_hint="--config"
        ) from None
    if not isinstance(data, dict):
        raise typer.BadParameter(
            "--config must be a JSON object.", param_hint="--config"
        )
    return data


def _body_view(record: dict[str, Any]) -> dict[str, Any]:
    """The canonical content triple used for diffing."""
    return {
        "template": record["template"],
        "variables": record["variables"],
        "config": record["config"],
    }


def _short_hash(content_hash: str) -> str:
    return content_hash.removeprefix("sha256:")[:12]


@prompt_app.command("register")
def prompt_register_cmd(
    prompt_id: Annotated[
        str, typer.Argument(help="Stable prompt identity (lowercase slug).")
    ],
    template: Annotated[
        Optional[str],
        typer.Option("--template", "-t", help="Inline text-form template."),
    ] = None,
    file: Annotated[
        Optional[Path],
        typer.Option(
            "--file",
            "-f",
            help=(
                "Template file: .json = chat-form array of {role, content}; "
                "anything else = text-form, read verbatim."
            ),
        ),
    ] = None,
    var: Annotated[
        Optional[list[str]],
        typer.Option(
            "--var",
            help="Declared placeholder name (repeatable). Documentation only — never rendered.",
        ),
    ] = None,
    config: Annotated[
        Optional[str],
        typer.Option(
            "--config",
            help='Model-agnostic hints as a JSON object, e.g. \'{"temperature": 0.2}\'.',
        ),
    ] = None,
    message: Annotated[
        str,
        typer.Option("--message", "-m", help="Commit message (the version's 'why')."),
    ] = "",
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the raw record as JSON.")
    ] = False,
) -> None:
    """Register a new immutable prompt version (experimental, ADR-0112).

    Auto-increments the version and computes the content hash over the
    canonical body. Re-registering identical content is idempotent — the
    existing version is returned and no new row is written.

    Scope: one prompt version.

    \b
    Examples:
      nova prompt register triage -t "You triage {ticket_body}" --var ticket_body -m "first cut"
      nova prompt register triage -f messages.json --config '{"temperature": 0.2}'
    """
    from novafabric.registry.composition import CompositionError
    from novafabric.registry.prompts import (
        PromptRegistrationError,
        frozen_ref,
        register_prompt_version,
    )

    body = _load_template(template, file)
    cfg = _parse_config(config)
    variables = list(var or [])

    try:
        record, created = register_prompt_version(
            prompt_id=prompt_id,
            template=body,
            variables=variables,
            config=cfg,
            commit_message=message,
        )
    # pydantic.ValidationError subclasses ValueError, so both a malformed
    # template/slug and an empty chat form land here. CompositionError is
    # the ADR-0115 register-time gate (cycle / depth / unknown ref).
    except (ValueError, PromptRegistrationError, CompositionError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(record, indent=2, ensure_ascii=False))
        return

    for warning in variable_mismatch_warnings(body, variables):
        console.print(f"[yellow]warning:[/yellow] {warning}")

    ref = frozen_ref(record)
    if created:
        console.print(
            f"[green]✓[/green] Registered prompt "
            f"{record['prompt_id']}@{record['version']} "
            f"({_short_hash(record['content_hash'])}…)"
        )
    else:
        console.print(
            f"[yellow]=[/yellow] Unchanged content — existing version "
            f"{record['prompt_id']}@{record['version']} returned "
            f"({_short_hash(record['content_hash'])}…)"
        )
    # Machine-copyable ref must bypass Rich: console.print soft-wraps at
    # terminal width and can break the sha mid-line (same class of bug as the
    # nova diff JSON fix in 0.59.0).
    typer.echo(f"  ref: {ref}")
    for entry in record.get("composition", []):
        console.print(
            f"  includes: {entry['ref']} → v{entry['resolved_version']} "
            f"({_short_hash(entry['resolved_hash'])}…) "
            f"\\[{entry.get('selector_kind', 'version')}]"
        )


@prompt_app.command("get")
def prompt_get_cmd(
    ref: Annotated[
        str,
        typer.Argument(help="Prompt ref: <prompt_id> (latest) or <prompt_id>@<version>."),
    ],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the raw record as JSON.")
    ] = False,
) -> None:
    """Fetch one prompt version, frozen to version + content hash.

    Prints the replay-verifiable reference
    ``prompt:<id>@<version>+sha256:<hex>`` so the resolved version can be
    pinned in a capsule.

    Scope: one prompt version.

    \b
    Examples:
      nova prompt get triage
      nova prompt get triage@3 --json
    """
    from novafabric.registry.prompts import (
        PromptNotFoundError,
        frozen_ref,
        get_prompt_version,
    )

    prompt_id, version = _parse_ref(ref)
    try:
        record = get_prompt_version(prompt_id, version)
    except PromptNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(record, indent=2, ensure_ascii=False))
        return

    lines = [
        f"[bold]ref:[/bold] {frozen_ref(record)}",
        f"[bold]status:[/bold] {record['status']}",
        f"[bold]created_at:[/bold] {record['created_at']}",
        f"[bold]commit_message:[/bold] {record['commit_message']}",
        f"[bold]variables:[/bold] {json.dumps(record['variables'])}",
        f"[bold]config:[/bold] {json.dumps(record['config'], ensure_ascii=False)}",
        "[bold]template:[/bold]",
        json.dumps(record["template"], indent=2, ensure_ascii=False)
        if not isinstance(record["template"], str)
        else record["template"],
    ]
    console.print(
        Panel("\n".join(lines), title=f"{record['prompt_id']}@{record['version']}")
    )


@prompt_app.command("list")
def prompt_list_cmd(
    prompt_id: Annotated[
        Optional[str],
        typer.Argument(help="Optional prompt id — list its versions instead."),
    ] = None,
    status: Annotated[
        Optional[str],
        typer.Option("--status", "-s", help="Filter by lifecycle status."),
    ] = None,
) -> None:
    """List managed prompts, or all versions of one prompt.

    Scope: registry-wide (or one prompt with an argument).

    \b
    Examples:
      nova prompt list
      nova prompt list triage --status production
    """
    from novafabric.registry.prompts import (
        PromptNotFoundError,
        list_prompt_versions,
        list_prompts,
    )

    if prompt_id is None:
        summaries = list_prompts(status=status)
        if not summaries:
            console.print("No managed prompts registered.")
            return
        table = Table(title="Prompts")
        table.add_column("Prompt", style="cyan")
        table.add_column("Latest", justify="right")
        table.add_column("Status", style="yellow")
        table.add_column("Versions", justify="right")
        table.add_column("Content hash")
        for s in summaries:
            table.add_row(
                s["prompt_id"],
                str(s["latest_version"]),
                s["status"],
                str(s["versions"]),
                _short_hash(s["content_hash"]) + "…",
            )
        console.print(table)
        return

    try:
        records = list_prompt_versions(prompt_id, status=status)
    except PromptNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    table = Table(title=f"Versions of '{prompt_id}'")
    table.add_column("Version", justify="right")
    table.add_column("Status", style="yellow")
    table.add_column("Created at")
    table.add_column("Content hash")
    for r in records:
        table.add_row(
            str(r["version"]),
            r["status"],
            str(r["created_at"])[:19],
            _short_hash(r["content_hash"]) + "…",
        )
    console.print(table)


@prompt_app.command("history")
def prompt_history_cmd(
    prompt_id: Annotated[str, typer.Argument(help="Prompt id.")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the full version records as JSON.")
    ] = False,
) -> None:
    """Chronological version log with commit messages and content hashes.

    Scope: one prompt, all versions.

    \b
    Examples:
      nova prompt history triage
    """
    from novafabric.registry.prompts import PromptNotFoundError, list_prompt_versions

    try:
        records = list_prompt_versions(prompt_id)
    except PromptNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(records, indent=2, ensure_ascii=False))
        return

    table = Table(title=f"History of '{prompt_id}'")
    table.add_column("Version", justify="right")
    table.add_column("Created at")
    table.add_column("Status", style="yellow")
    table.add_column("Content hash")
    table.add_column("Commit message")
    for r in records:
        table.add_row(
            str(r["version"]),
            str(r["created_at"])[:19],
            r["status"],
            _short_hash(r["content_hash"]) + "…",
            r["commit_message"],
        )
    console.print(table)


@prompt_app.command("diff")
def prompt_diff_cmd(
    ref_a: Annotated[
        str, typer.Argument(help="First ref in <prompt_id>@<version> form.")
    ],
    ref_b: Annotated[
        str, typer.Argument(help="Second ref in <prompt_id>@<version> form.")
    ],
    unified: Annotated[
        int,
        typer.Option("--unified", "-U", help="Lines of context in unified diff output."),
    ] = 3,
    output_format: Annotated[
        str,
        typer.Option("--output-format", help="Output format: unified|json."),
    ] = "unified",
) -> None:
    """Structural diff of template/variables/config between two versions.

    Exits ``1`` when the versions differ (CI-gateable), ``0`` when identical.

    Scope: two prompt versions.

    \b
    Examples:
      nova prompt diff triage@6 triage@7
      nova prompt diff triage@6 triage@7 --output-format json
    """
    from novafabric.registry.prompts import PromptNotFoundError, get_prompt_version

    records = []
    for ref in (ref_a, ref_b):
        prompt_id, version = _parse_ref(ref, require_version=True)
        try:
            records.append(get_prompt_version(prompt_id, version))
        except PromptNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None
    body_a, body_b = _body_view(records[0]), _body_view(records[1])

    if output_format == "json":
        flat_a, flat_b = _flatten(body_a), _flatten(body_b)
        all_keys = set(flat_a) | set(flat_b)
        added = {k: flat_b[k] for k in sorted(all_keys) if k not in flat_a}
        removed = {k: flat_a[k] for k in sorted(all_keys) if k not in flat_b}
        changed = {
            k: {"from": flat_a[k], "to": flat_b[k]}
            for k in sorted(all_keys)
            if k in flat_a and k in flat_b and flat_a[k] != flat_b[k]
        }
        typer.echo(
            json.dumps(
                {
                    "ref_a": ref_a,
                    "ref_b": ref_b,
                    "identical": not (added or removed or changed),
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        if added or removed or changed:
            raise typer.Exit(code=1)
        return

    lines_a = json.dumps(
        body_a, indent=2, sort_keys=True, ensure_ascii=False
    ).splitlines(keepends=True)
    lines_b = json.dumps(
        body_b, indent=2, sort_keys=True, ensure_ascii=False
    ).splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(lines_a, lines_b, fromfile=ref_a, tofile=ref_b, n=unified)
    )
    if not diff_lines:
        console.print("[green]No differences found.[/green]")
        return
    for line in diff_lines:
        stripped = line.rstrip("\n")
        if stripped.startswith(("+++", "---")):
            console.print(f"[bold]{stripped}[/bold]")
        elif stripped.startswith("+"):
            console.print(f"[green]{stripped}[/green]")
        elif stripped.startswith("-"):
            console.print(f"[red]{stripped}[/red]")
        elif stripped.startswith("@@"):
            console.print(f"[cyan]{stripped}[/cyan]")
        else:
            console.print(stripped)
    raise typer.Exit(code=1)


@prompt_app.command("compose")
def prompt_compose_cmd(
    ref: Annotated[
        str,
        typer.Argument(
            help=(
                "Prompt ref: <prompt_id>, <prompt_id>@<version>, or "
                "<prompt_id>@<label> (latest version when bare)."
            )
        ),
    ],
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the raw resolved_composition_manifest as JSON."),
    ] = False,
    assembled: Annotated[
        bool,
        typer.Option(
            "--assembled",
            help="Print the fully spliced template instead of the manifest.",
        ),
    ] = False,
) -> None:
    """Resolve a prompt's full composition DAG (experimental, ADR-0115).

    Expands every ``{{@prompt:<name>@<selector>}}`` reference transitively
    and prints the flattened, content-addressed manifest: every included
    prompt version + hash, the resolved edges, and the hash of the final
    assembled prompt. Read-only — nothing is captured or written. Label
    selectors resolve at this instant; the manifest records the outcome.

    Scope: one prompt version and its whole composition tree.

    \b
    Examples:
      nova prompt compose triage-agent
      nova prompt compose triage-agent@production --json
      nova prompt compose triage-agent@9 --assembled
    """
    from novafabric.registry.composition import (
        CompositionError,
        resolve_composition,
    )
    from novafabric.registry.prompts import PromptNotFoundError

    prompt_id, selector = _parse_selector_ref(ref)
    try:
        manifest, assembled_template = resolve_composition(prompt_id, selector)
    except (PromptNotFoundError, CompositionError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    if json_output:
        typer.echo(json.dumps(manifest, indent=2, ensure_ascii=False))
        return
    if assembled:
        if isinstance(assembled_template, str):
            typer.echo(assembled_template)
        else:
            typer.echo(json.dumps(assembled_template, indent=2, ensure_ascii=False))
        return

    root = manifest["root"]
    table = Table(
        title=f"Composition of {root['name']}@{root['version']} "
        f"({_short_hash(root['hash'])}…)"
    )
    table.add_column("Depth", justify="right")
    table.add_column("Prompt", style="cyan")
    table.add_column("Version", justify="right")
    table.add_column("Content hash")
    for node in sorted(manifest["included"], key=lambda n: (n["depth"], n["name"])):
        table.add_row(
            str(node["depth"]),
            node["name"],
            node["version"],
            _short_hash(node["hash"]) + "…",
        )
    console.print(table)
    console.print(f"[bold]edges:[/bold] {len(manifest['edges'])}")
    console.print(f"[bold]max_depth:[/bold] {manifest['max_depth']}")
    console.print(
        f"[bold]assembled_prompt_hash:[/bold] {manifest['assembled_prompt_hash']}"
    )
    console.print(f"[bold]resolved_at:[/bold] {manifest['resolved_at']}")


@prompt_app.command("tree")
def prompt_tree_cmd(
    ref: Annotated[
        str,
        typer.Argument(
            help=(
                "Prompt ref: <prompt_id>, <prompt_id>@<version>, or "
                "<prompt_id>@<label> (latest version when bare)."
            )
        ),
    ],
) -> None:
    """Print a prompt's composition DAG as an indented tree (ADR-0115).

    Each node shows the reference as written, the version it resolved to,
    its content hash, and a ``[label]`` flag when the reference was pinned
    through a deployment label. Read-only.

    Scope: one prompt version and its whole composition tree.

    \b
    Examples:
      nova prompt tree triage-agent
      nova prompt tree triage-agent@9
    """
    from novafabric.registry.composition import (
        CompositionError,
        resolve_composition,
    )
    from novafabric.registry.prompts import PromptNotFoundError

    prompt_id, selector = _parse_selector_ref(ref)
    try:
        manifest, _ = resolve_composition(prompt_id, selector)
    except (PromptNotFoundError, CompositionError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    children: dict[str, list[dict[str, Any]]] = {}
    for edge in manifest["edges"]:
        children.setdefault(edge["parent_hash"], []).append(edge)

    root = manifest["root"]
    console.print(
        f"{root['name']}@{root['version']}  {_short_hash(root['hash'])}…"
    )

    def _print_children(parent_hash: str, prefix: str) -> None:
        edges = children.get(parent_hash, [])
        for i, edge in enumerate(edges):
            last = i == len(edges) - 1
            connector = "└── " if last else "├── "
            # edge ref form: @prompt:<name>@<selector>
            _, _, tail = edge["ref"].partition(":")
            name, _, selector_str = tail.rpartition("@")
            label_flag = (
                "  [yellow]\\[label][/yellow]"
                if edge.get("selector_kind") == "label"
                else ""
            )
            console.print(
                f"{prefix}{connector}{name}  @{selector_str} → "
                f"v{edge['resolved_version']}  "
                f"{_short_hash(edge['resolved_hash'])}…{label_flag}"
            )
            _print_children(
                edge["resolved_hash"], prefix + ("    " if last else "│   ")
            )

    _print_children(root["hash"], "")


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts/lists into dotted-path keys for structured diff."""
    result: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten(v, key))
        elif isinstance(v, list):
            for i, item in enumerate(v):
                item_key = f"{key}[{i}]"
                if isinstance(item, dict):
                    result.update(_flatten(item, item_key))
                else:
                    result[item_key] = item
        else:
            result[key] = v
    return result


@prompt_app.command("promote")
def prompt_promote(
    prompt_ref: str = typer.Argument(..., help="prompt_id@version"),
    to: AssetStatus = typer.Option(
        ..., "--to", help="Target status: development | staging | production."
    ),
    force: bool = typer.Option(
        False, "--force", help="Bypass the eval gate (requires typed confirmation)."
    ),
) -> None:
    """Promote a prompt version — a thin alias for `nova promote direct` (ADR-0112 P4).

    Deliberately an alias, not a second promotion path: prompts are ordinary
    registry assets, so they go through the same eval gate, the same policy
    gate, the same audit entry and the same lifecycle transitions as every
    other asset type. A separate implementation would be a second place for
    those gates to drift out of step.

    The one thing it adds is a type check: promoting a non-prompt asset
    through the prompt surface is refused, so the command means what its name
    says.

    \b
    Examples:
      nova prompt promote triage@1.2.0 --to staging
      nova prompt promote triage@1.2.0 --to production
    """
    from novafabric.registry.service import (
        AssetNotFoundError,
        InvalidLifecycleTransitionError,
        PromotionBlockedError,
        get_asset,
        promote_asset,
    )

    if "@" not in prompt_ref:
        typer.echo("Error: expected prompt_id@version (e.g. triage@1.2.0)", err=True)
        raise typer.Exit(code=2)
    name, version = prompt_ref.split("@", 1)

    # Refuse a non-prompt asset rather than silently promoting it: the command
    # is named for prompts and must not become a general back door.
    try:
        asset = get_asset(name, version)
    except AssetNotFoundError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    asset_type = str(asset.get("asset_type", "")) if isinstance(asset, dict) else ""
    if asset_type and asset_type != "prompt":
        typer.echo(
            f"Error: {name}@{version} is an asset of type {asset_type!r}, not 'prompt'. "
            f"Use `nova promote direct` for other asset types.",
            err=True,
        )
        raise typer.Exit(code=2)

    if force:
        confirmed = typer.prompt(
            f"Type the prompt name '{name}' to confirm forced promotion"
        )
        if confirmed != name:
            typer.echo("Confirmation failed. Aborting.", err=True)
            raise typer.Exit(code=1)

    try:
        result = promote_asset(name, version, to, actor="cli-user", force=force)
    except (
        AssetNotFoundError,
        InvalidLifecycleTransitionError,
        PromotionBlockedError,
    ) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    tag = " (forced)" if result.get("forced_promotion") else ""
    typer.echo(f"Promoted {name}@{version} → {result['status']}{tag}")
