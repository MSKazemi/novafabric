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

"""``nova settlement`` — settlement-provenance evidence (ADR-0163, experimental).

Ships ``nova settlement chain`` (NF-315, ADR-0163 P3): an offline walk of the
A2A payment-provenance chain stored in ``facets.settlement``. The other
subcommands ADR-0163 names (``bind``, ``show``, ``verify``, ``reconcile``,
``finality``, ``reversals``) remain planned.

Read-only: ``--capsule`` is never written. Nothing here moves, holds, or
releases value, contacts a network or PSP, or decides a dispute.

Streams: verdict JSON (``--json``) or the human walk on stdout; errors and the
in-mission-boundary line on stderr.

Exit codes: 0 the walk passed; 1 the evidence is broken or absent (a failing
walk, no chain, a malformed or secret-bearing facet — fail-closed); 2 bad
input (flags, unreadable/unparseable file, no such capsule).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref

console = Console(soft_wrap=True)
err_console = Console(stderr=True, soft_wrap=True)

#: Spec §3 req. 4: every ``nova settlement`` output carries this line.
BOUNDARY_LINE = (
    "NovaFabric records settlement provenance only; it never processes, holds, "
    "moves, or releases money, contacts no payment network, and adjudicates no "
    "transaction or dispute (ADR-0163 I-1, I-4)."
)

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2

#: A settlement facet is metadata; anything larger is not one (bounded read).
MAX_INPUT_BYTES = 16 * 1024 * 1024

settlement_app = typer.Typer(
    no_args_is_help=True,
    help="Settlement-provenance evidence (ADR-0163, experimental, record-only): "
    "A2A payment-chain walk.",
)


@settlement_app.callback()
def _settlement_group() -> None:
    """Settlement-provenance evidence (ADR-0163, experimental, record-only)."""


class _InputError(Exception):
    """A user-input problem, mapped to exit code 2."""


class _BrokenEvidence(Exception):
    """The stored evidence is absent or cannot be trusted, mapped to exit 1."""


def _describe(exc: Exception) -> str:
    """Render an error without echoing the rejected input (it may be a secret)."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)
        )
    return str(exc)


def _read_bounded(path: Path, what: str) -> str:
    try:
        size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            raise _InputError(f"{what} {path} is {size} bytes, over the {MAX_INPUT_BYTES} limit")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise _InputError(f"cannot read {what} {path}: {exc}") from exc


def _load_block(capsule: str | None, facet_path: Path | None) -> dict[str, Any]:
    """Return the raw ``facets.settlement`` block from exactly one source."""
    if (capsule is None) == (facet_path is None):
        raise _InputError("pass exactly one of --capsule or --facet")
    if facet_path is not None:
        try:
            block = json.loads(_read_bounded(facet_path, "facet file"))
        except json.JSONDecodeError as exc:
            raise _InputError(f"cannot parse facet file {facet_path}: {exc}") from exc
        if not isinstance(block, dict):
            raise _InputError(f"facet file {facet_path} is not a JSON object")
        return block
    try:
        capsule_dir = resolve_capsule_ref(str(capsule))
    except CapsuleRefError as exc:
        raise _InputError(str(exc)) from exc
    try:
        manifest = yaml.safe_load(_read_bounded(capsule_dir / "capsule.yaml", "capsule manifest"))
    except yaml.YAMLError as exc:
        raise _InputError(f"cannot parse capsule.yaml in {capsule_dir}: {exc}") from exc
    facets = manifest.get("facets") if isinstance(manifest, dict) else None
    block = facets.get("settlement") if isinstance(facets, dict) else None
    if block is None:
        raise _BrokenEvidence(f"capsule {capsule_dir} has no facets.settlement")
    if not isinstance(block, dict):
        raise _BrokenEvidence("facets.settlement is not a mapping")
    return block


def _hop_view(hop: Any) -> dict[str, Any]:
    return {
        "hop_index": hop.hop_index,
        "parent_hop": hop.parent_hop,
        "payer_agent_ref": hop.payer_agent_ref,
        "payee_agent_ref": hop.payee_agent_ref,
        "amount_minor": hop.amount.amount_minor,
        "currency": hop.amount.currency,
        "settlement_ref": hop.settlement_ref,
    }


@settlement_app.command("chain")
def chain_cmd(
    capsule: Annotated[
        str | None,
        typer.Option(
            "--capsule",
            help="Capsule directory or run id whose facets.settlement to read (read-only).",
            show_default=False,
        ),
    ] = None,
    facet: Annotated[
        Path | None,
        typer.Option(
            "--facet",
            help="A standalone settlement-facet JSON document to read instead of a capsule.",
            show_default=False,
        ),
    ] = None,
    depth: Annotated[
        int | None,
        typer.Option(
            "--depth",
            min=1,
            help="Show at most N hops, newest first, following parent_hop back. "
            "Display only: the verdict always covers the whole chain.",
            show_default=False,
        ),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="Emit the walk as JSON.")] = False,
) -> None:
    """Walk the A2A payment-provenance chain offline (NF-315).

    Checks that every parent_hop resolves to an earlier hop (acyclic, no
    broken parent, no fork), that hop_index is the recorded order, that each
    payer is its parent's payee, and that currencies agree. Moves no value.

    \b
    Examples:
      nova settlement chain --capsule 01HXAY7M5JZ8R7K4P9DPBYK2WX
      nova settlement chain --facet settlement.json --depth 3 --json
    """
    from novafabric.settlement import (
        MAX_CHAIN_HOPS,
        BrokenA2AChainError,
        InvalidIdentityRefError,
        InvalidReferenceError,
        MalformedA2AChainError,
        PaymentSecretRejectedError,
        SettlementFacet,
        chain_from_facet,
        verify_a2a_chain,
        walk_back,
    )

    code = EXIT_OK
    try:
        block = _load_block(capsule, facet)
        try:
            settlement = SettlementFacet.model_validate(block)
            hops = chain_from_facet(settlement)
        except ValidationError as exc:
            raise _BrokenEvidence(f"malformed facets.settlement: {_describe(exc)}") from exc
        except InvalidReferenceError as exc:
            raise _BrokenEvidence(
                "facets.settlement holds a reference that is not a sha256 digest"
            ) from exc
        except (
            MalformedA2AChainError,
            BrokenA2AChainError,
            InvalidIdentityRefError,
            PaymentSecretRejectedError,
        ) as exc:
            raise _BrokenEvidence(str(exc)) from exc
        if hops is None:
            raise _BrokenEvidence("facets.settlement records no a2a_payment_chain")
        verification = verify_a2a_chain(hops)
        shown = walk_back(hops, depth if depth is not None else MAX_CHAIN_HOPS)
        if json_out:
            payload = verification.model_dump(mode="json")
            payload["ok"] = verification.ok
            payload["walk"] = [_hop_view(hop) for hop in shown]
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            _print_walk(verification, shown)
        code = EXIT_OK if verification.ok else EXIT_BROKEN
    except _BrokenEvidence as exc:
        err_console.print(f"[red]Broken:[/red] {exc}", highlight=False)
        code = EXIT_BROKEN
    except _InputError as exc:
        err_console.print(f"[red]Error:[/red] {exc}", highlight=False)
        code = EXIT_USAGE
    err_console.print(f"[dim]{BOUNDARY_LINE}[/dim]", highlight=False)
    raise typer.Exit(code)


def _print_walk(verification: Any, shown: list[Any]) -> None:
    """Human rendering of a walk: verdicts, findings, then the hops shown."""
    verdict = "[green]ok[/green]" if verification.ok else "[red]BROKEN[/red]"
    console.print(f"a2a_payment_chain: {verdict} ({verification.hop_count} hops)")
    for name in (
        "ordered",
        "no_broken_parent",
        "acyclic",
        "linear",
        "continuous",
        "currency_consistent",
    ):
        console.print(f"  {name}: {str(getattr(verification, name)).lower()}")
    console.print("  moved_value: false")
    for finding in verification.findings:
        console.print(f"  finding[{finding.position}] {finding.code}: {finding.message}")
    console.print(f"walk (newest first, {len(shown)} shown):")
    for hop in shown:
        view = _hop_view(hop)
        console.print(
            f"  #{view['hop_index']} {view['payer_agent_ref']} -> {view['payee_agent_ref']} "
            f"{view['amount_minor']} {view['currency']} (minor units) "
            f"parent={view['parent_hop']} ref={view['settlement_ref']}",
            highlight=False,
        )
