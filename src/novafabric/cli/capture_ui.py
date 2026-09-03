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

"""``nova capture-ui`` — read computer-use actions and observations (ADR-0148 D3).

Two read surfaces over the NF-166/167 facets. Neither writes, and neither drives a browser:
NovaFabric records that a GUI action happened; performing one is acting, not recording, and
the ADR rejects it as scope.

**Exit codes.** ``0`` when the command did its job, including a ``verify`` that finds an
observation whose bytes no longer match — reporting that *is* the job succeeding. ``1`` only
for ``verify --strict``, where the caller asked for a gate. ``2`` is a usage error.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console

from novafabric.capture.ui import KEYSTROKE_RESIDUAL_RISK

app = typer.Typer(
    name="capture-ui",
    help=(
        "Computer-use evidence: GUI actions and what the agent saw "
        "(experimental, ADR-0148 D3)."
    ),
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

HONESTY_LINE = (
    "NovaFabric records that these GUI actions were declared. It does not perform, replay, "
    "or verify that they took effect."
)


def _capsule_manifest(capsule: Path) -> dict[str, Any]:
    if not capsule.is_dir():
        err_console.print(f"[red]Capsule directory not found:[/red] {capsule}")
        raise typer.Exit(2)
    path = capsule / "capsule.yaml"
    if not path.exists():
        err_console.print(f"[red]capsule.yaml not found in[/red] {capsule}")
        raise typer.Exit(2)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        err_console.print(f"[red]Could not read capsule.yaml:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(data, dict):
        err_console.print("[red]capsule.yaml is not a mapping.[/red]")
        raise typer.Exit(2)
    return data


@app.command("show")
def show(
    capsule: Annotated[Path, typer.Option("--capsule", help="Capsule directory.")],
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit both facets as JSON.")
    ] = False,
) -> None:
    """Print the recorded GUI actions and observations (NF-166/167).

    Typed text is shown as a **salted digest** or as ``redacted`` — never as raw
    keystrokes unless byte capture was opted in and the text survived redaction.

    \b
    Examples:
      nova capture-ui show --capsule runs/run_1
      nova capture-ui show --capsule runs/run_1 --json
    """
    from novafabric.capture.ui import actions_from_capsule, observations_from_capsule

    manifest = _capsule_manifest(capsule)
    actions = actions_from_capsule(manifest)
    observations = observations_from_capsule(manifest)

    if json_out:
        print(
            json.dumps(
                {
                    "ui_actions": actions.model_dump(mode="json", exclude_none=True)
                    if actions
                    else None,
                    "ui_observations": observations.model_dump(
                        mode="json", exclude_none=True
                    )
                    if observations
                    else None,
                    "honesty": HONESTY_LINE,
                },
                indent=2,
            )
        )
        raise typer.Exit(0)

    if actions is None and observations is None:
        console.print("No computer-use evidence in this capsule.")
        console.print(f"[dim]{HONESTY_LINE}[/dim]")
        raise typer.Exit(0)

    if actions is not None:
        console.print(
            f"ui_actions v{actions.schema_version} — {len(actions.actions)} action(s)"
            + (f", [red]{actions.dropped} dropped[/red]" if actions.dropped else "")
        )
        for act in actions.actions:
            detail = ""
            if act.typed is not None:
                if act.typed.redacted:
                    detail = f" text=[yellow]redacted[/yellow] ({act.typed.redaction_reason})"
                elif act.typed.text_digest:
                    detail = f" text_digest={act.typed.text_digest[:19]}…"
            console.print(
                f"  {act.action_seq:>3} {act.kind:<10} "
                f"{act.target_ref or act.url or '—'}{detail}"
            )
        if actions.text_digest_salt:
            console.print(f"[dim]{KEYSTROKE_RESIDUAL_RISK}[/dim]")

    if observations is not None:
        console.print(
            f"ui_observations v{observations.schema_version} — "
            f"{len(observations.observations)} observation(s)"
            + (
                f", [red]{observations.dropped} dropped[/red]"
                if observations.dropped
                else ""
            )
        )
        for obs in observations.observations:
            console.print(
                f"  {obs.obs_seq:>3} {obs.kind:<13} {obs.content_hash[:19]}… "
                f"{obs.byte_size}B blob={'yes' if obs.blob_ref else 'reference-only'}"
            )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")


@app.command("verify")
def verify(
    capsule: Annotated[Path, typer.Option("--capsule", help="Capsule directory.")],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit 1 if any stored observation's bytes no longer match its hash.",
        ),
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the result as JSON.")
    ] = False,
) -> None:
    """Check that stored observation bytes still hash to their recorded ``content_hash``.

    An observation with no ``blob_ref`` is **not** a failure — reference-metadata-only is
    the documented default, so there is nothing to resolve. Counting it as unresolved would
    make every privacy-preserving capsule look corrupt.

    \b
    Examples:
      nova capture-ui verify --capsule runs/run_1
      nova capture-ui verify --capsule runs/run_1 --strict
    """
    from novafabric.capture.ui import observations_from_capsule, unresolved_observations

    manifest = _capsule_manifest(capsule)
    facet = observations_from_capsule(manifest)

    if facet is None:
        if json_out:
            print(json.dumps({"ui_observations": None, "unresolved": []}, indent=2))
        else:
            console.print("No ui_observations facet to verify.")
            console.print(f"[dim]{HONESTY_LINE}[/dim]")
        raise typer.Exit(0)

    bad = unresolved_observations(facet, capsule)
    stored = [o for o in facet.observations if o.blob_ref]

    if json_out:
        print(
            json.dumps(
                {
                    "observations": len(facet.observations),
                    "with_stored_bytes": len(stored),
                    "unresolved": [o.content_hash for o in bad],
                    "honesty": HONESTY_LINE,
                },
                indent=2,
            )
        )
        raise typer.Exit(1 if (strict and bad) else 0)

    console.print(
        f"{len(facet.observations)} observation(s), {len(stored)} with stored bytes, "
        f"{len(bad)} unresolved."
    )
    for obs in bad:
        console.print(f"  [red]unresolved[/red] {obs.content_hash[:19]}… {obs.blob_ref}")
    if not stored:
        console.print(
            "[dim]All observations are reference-metadata-only — nothing to resolve, "
            "which is the default, not a fault.[/dim]"
        )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")
    raise typer.Exit(1 if (strict and bad) else 0)
