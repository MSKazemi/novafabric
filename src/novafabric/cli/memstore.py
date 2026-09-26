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

"""``nova memstore`` — shared-store governance evidence, offline (ADR-0171).

Experimental. P2 ships the three surfaces ADR-0171 §CLI names for NF-392/395/396:

- ``nova memstore access ledger`` — who read/wrote which entry, and whether it
  was inside the agent's declared scope; ``--uncontained`` lists the
  out-of-scope rows. ``contained: false`` is evidence, never enforcement.
- ``nova memstore derive`` — back-trace a read in one run to the store-write
  that seeded it (``origin_mutation_ref`` + ``origin_run``), then onward
  through the origin run's earlier reads; bounded and cycle-safe.
- ``nova memstore provenance`` — the ``source → store-write → later runs``
  fan-out for one entry.

All three read an **explicit list of capsules** (``--capsule``, repeatable) —
there is no cross-run index — and never write anything. ``derive`` and
``provenance`` may take the sealed mutation-ledger sidecar via ``--ledger``
instead of re-assembling the chain from the capsules' facets. Every output
carries the in-mission-boundary line.

**Exit codes.** ``0`` — evidence read and reported (an out-of-scope access is
*evidence*, not a failure). ``1`` — the recorded evidence is defective (broken
access chain, forged ``contained`` flag, malformed block, broken ``--ledger``
chain) or, for ``derive``, a root read could not be bound to a recorded write
(``unresolved`` / ``claim_mismatch``). ``2`` — nothing could be checked: a
capsule or ledger could not be read, or there was nothing to trace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from novafabric.memstore import (
    IN_MISSION_BOUNDARY,
    AccessRecord,
    DerivationBoundError,
    LedgerAssembly,
    MemstoreError,
    MutationRecord,
    StoreEvidence,
    assemble_ledger,
    collect_store_evidence,
    ledger_from_sidecar,
    provenance_chain,
    trace_derivation,
)
from novafabric.memstore.derivation import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_NODES,
    MAX_CAPSULES,
    MAX_LEDGER_RECORDS,
)

memstore_app = typer.Typer(
    name="memstore",
    help=(
        "Shared-store governance evidence: access ledger, cross-run derivation, "
        "provenance fan-out — offline over explicit capsules (experimental, "
        "ADR-0171). Records evidence about the store; never hosts, serves, "
        "manages, or gates it."
    ),
    no_args_is_help=True,
)
access_app = typer.Typer(
    name="access",
    help="NF-392 access-governance ledger (contained:false is evidence, not enforcement).",
    no_args_is_help=True,
)
memstore_app.add_typer(access_app, name="access")

console = Console()
err_console = Console(stderr=True)

_MANIFEST_NAME = "capsule.yaml"
#: Largest capsule manifest or ledger sidecar this command will parse.
MAX_INPUT_BYTES = 16 * 1024 * 1024

CapsulesOpt = Annotated[
    list[str],
    typer.Option(
        "--capsule",
        help="Capsule directory or bare run id; repeat for each run in the set.",
    ),
]
StoreOpt = Annotated[str, typer.Option("--store", help="Store id the evidence is about.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]
NamespaceOpt = Annotated[str | None, typer.Option("--namespace", help="Restrict to one namespace.")]
LedgerOpt = Annotated[
    Path | None,
    typer.Option(
        "--ledger",
        help="Sealed mutation-ledger sidecar (JSON list, or {'ledger': [...]}) used "
        "instead of re-assembling the chain from the capsules.",
    ),
]


# ── Input loading (read-only, bounded) ────────────────────────────────────


def _fail(message: str) -> NoReturn:
    err_console.print(f"[red]{escape(message)}[/red]")
    raise typer.Exit(2)


def _read_bounded(path: Path, what: str) -> str:
    try:
        size = path.stat().st_size
        if size > MAX_INPUT_BYTES:
            _fail(f"{what} is {size} bytes, over the {MAX_INPUT_BYTES}-byte limit")
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _fail(f"Could not read {what}: {exc}")


def _load_manifests(capsules: list[str]) -> list[dict[str, Any]]:
    """Resolve and parse every ``--capsule``; exit 2 on any unreadable one."""
    from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref

    if not capsules:
        _fail("Supply at least one --capsule.")
    if len(capsules) > MAX_CAPSULES:
        _fail(f"Over {MAX_CAPSULES} capsules supplied.")
    manifests: list[dict[str, Any]] = []
    for ref in capsules:
        try:
            capsule_dir = resolve_capsule_ref(ref)
        except CapsuleRefError as exc:
            _fail(str(exc))
        text = _read_bounded(capsule_dir / _MANIFEST_NAME, f"{ref}/{_MANIFEST_NAME}")
        try:
            manifest = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            _fail(f"Could not parse {ref}/{_MANIFEST_NAME}: {exc}")
        if not isinstance(manifest, dict):
            _fail(f"{ref}/{_MANIFEST_NAME} is not a mapping.")
        manifests.append(manifest)
    return manifests


def _load_sidecar(path: Path) -> list[MutationRecord]:
    """Parse an explicit ledger sidecar; exit 2 when unreadable, 1 when malformed."""
    try:
        doc = json.loads(_read_bounded(path, str(path)))
    except json.JSONDecodeError as exc:
        _fail(f"{path} is not JSON: {exc}")
    rows = doc.get("ledger") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        _fail(f"{path} holds no ledger list.")
    if len(rows) > MAX_LEDGER_RECORDS:
        _fail(f"{path} holds over {MAX_LEDGER_RECORDS} records.")
    try:
        return [MutationRecord.model_validate(r) for r in rows]
    except (ValidationError, MemstoreError) as exc:
        err_console.print(f"[red]Malformed ledger sidecar:[/red] {escape(str(exc))}")
        console.print(IN_MISSION_BOUNDARY)
        raise typer.Exit(1) from exc


def _collect(capsules: list[str], store: str) -> StoreEvidence:
    manifests = _load_manifests(capsules)
    try:
        return collect_store_evidence(manifests, store)
    except (DerivationBoundError, MemstoreError) as exc:
        _fail(str(exc))


def _assembly(evidence: StoreEvidence, ledger: Path | None) -> LedgerAssembly:
    try:
        if ledger is not None:
            return ledger_from_sidecar(_load_sidecar(ledger))
        return assemble_ledger(evidence.mutations)
    except DerivationBoundError as exc:
        _fail(str(exc))


def _emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _print_findings(evidence: StoreEvidence) -> None:
    for finding in evidence.findings:
        err_console.print(f"[yellow]finding:[/yellow] {escape(finding)}")


# ── nova memstore access ledger ───────────────────────────────────────────


def _access_row(record: AccessRecord) -> dict[str, Any]:
    return record.model_dump(exclude_none=True, exclude={"prev_record_hash", "schema_version"})


@access_app.command("ledger")
def access_ledger_cmd(
    capsule: CapsulesOpt,
    store: StoreOpt,
    agent: Annotated[str | None, typer.Option("--agent", help="Only this agent's rows.")] = None,
    only_uncontained: Annotated[
        bool,
        typer.Option("--uncontained", help="List only out-of-scope (contained:false) rows."),
    ] = False,
    json_out: JsonOpt = False,
) -> None:
    """Who read/wrote which entry, and whether it was in declared scope (NF-392).

    Out-of-scope rows are evidence, not failures: they never change the exit
    code. Exits 1 if a capsule's access block is malformed, its chain is broken,
    or a recorded ``contained`` flag disagrees with its scope.
    """
    evidence = _collect(capsule, store)
    rows = [
        r
        for r in evidence.accesses
        if (agent is None or r.agent == agent) and (not only_uncontained or not r.contained)
    ]
    if json_out:
        _emit_json(
            {
                "store_id": store,
                "accesses": [_access_row(r) for r in rows],
                "uncontained": sum(1 for r in rows if not r.contained),
                "findings": evidence.findings,
                "ok": not evidence.defective,
                "boundary": IN_MISSION_BOUNDARY,
            }
        )
    else:
        _print_findings(evidence)
        if not rows:
            console.print(f"No access rows recorded for store {escape(store)}.")
        else:
            table = Table(title=f"Access ledger — {escape(store)}")
            for col in ("run", "agent", "access", "namespace/entry", "scope", "contained"):
                table.add_column(col)
            for r in rows:
                table.add_row(
                    escape(r.run or "-"),
                    escape(r.agent),
                    r.access,
                    escape(f"{r.namespace}/{r.entry_id}"),
                    escape(r.allowed_scope),
                    "yes" if r.contained else "[red]NO (evidence)[/red]",
                )
            console.print(table)
        console.print(IN_MISSION_BOUNDARY)
    raise typer.Exit(1 if evidence.defective else 0)


# ── nova memstore derive ──────────────────────────────────────────────────


@memstore_app.command("derive")
def derive_cmd(
    entry: Annotated[str, typer.Option("--entry", help="Entry id that was read.")],
    run: Annotated[str, typer.Option("--run", help="Run id that read it.")],
    capsule: CapsulesOpt,
    store: StoreOpt,
    namespace: NamespaceOpt = None,
    ledger: LedgerOpt = None,
    max_depth: Annotated[
        int, typer.Option("--max-depth", help="Hops to follow upstream (0-64).")
    ] = DEFAULT_MAX_DEPTH,
    max_nodes: Annotated[
        int, typer.Option("--max-nodes", help="Maximum reads in the trace (1-10000).")
    ] = DEFAULT_MAX_NODES,
    json_out: JsonOpt = False,
) -> None:
    """Back-trace a read in RUN to the store-write that seeded it (NF-395).

    Follows the origin run's earlier reads upstream (co-occurrence, not proven
    causation), cycle-safe and bounded. Exits 1 if a root read cannot be bound
    to a recorded write or the evidence is defective; 2 if RUN recorded no read
    of the entry.
    """
    evidence = _collect(capsule, store)
    assembly = _assembly(evidence, ledger)
    try:
        trace = trace_derivation(
            store_id=store,
            entry_id=entry,
            run=run,
            assembly=assembly,
            accesses=evidence.accesses,
            namespace=namespace,
            max_depth=max_depth,
            max_nodes=max_nodes,
        )
    except DerivationBoundError as exc:
        _fail(str(exc))
    roots = [h for h in trace.hops if h.depth == 0]
    if not roots:
        _print_findings(evidence)
        _fail(f"Run {run} recorded no read of {entry} in store {store}.")
    root_ok = all(h.link.status in ("resolved", "claimed_only") for h in roots)
    sidecar_broken = ledger is not None and not assembly.chain_ok
    failed = evidence.defective or sidecar_broken or not root_ok
    if json_out:
        payload = trace.model_dump(exclude_none=True)
        payload.update(findings=evidence.findings, ok=not failed, boundary=IN_MISSION_BOUNDARY)
        _emit_json(payload)
    else:
        _print_findings(evidence)
        if not assembly.chain_ok:
            err_console.print(
                f"[yellow]ledger chain incomplete:[/yellow] {escape(assembly.reason or '')}"
            )
        for hop in trace.hops:
            link = hop.link
            indent = "  " * hop.depth
            origin = (
                f"← {link.origin_mutation_ref} (run {link.origin_run or '-'}, "
                f"agent {link.origin_agent})"
                if link.origin_mutation_ref
                else f"({link.reason})"
            )
            console.print(
                escape(
                    f"{indent}[{link.status}] {link.namespace}/{link.read_entry} "
                    f"read by {link.read_run} {origin}"
                )
            )
        if trace.truncated:
            console.print(f"[yellow]truncated:[/yellow] {escape(trace.truncated_reason or '')}")
        console.print(IN_MISSION_BOUNDARY)
    raise typer.Exit(1 if failed else 0)


# ── nova memstore provenance ──────────────────────────────────────────────


@memstore_app.command("provenance")
def provenance_cmd(
    entry: Annotated[str, typer.Option("--entry", help="Entry id to fan out.")],
    capsule: CapsulesOpt,
    store: StoreOpt,
    namespace: NamespaceOpt = None,
    ledger: LedgerOpt = None,
    json_out: JsonOpt = False,
) -> None:
    """Source → store-write → later runs for one entry (NF-396).

    Exits 2 if no write of the entry is recorded; 1 if the evidence is
    defective or a supplied ``--ledger`` chain is broken.
    """
    evidence = _collect(capsule, store)
    assembly = _assembly(evidence, ledger)
    chain = provenance_chain(
        store_id=store,
        entry_id=entry,
        assembly=assembly,
        accesses=evidence.accesses,
        namespace=namespace,
    )
    if not chain.writes:
        _print_findings(evidence)
        _fail(f"No recorded write of {entry} in store {store}.")
    failed = evidence.defective or (ledger is not None and not assembly.chain_ok)
    if json_out:
        payload = chain.model_dump(exclude_none=True)
        payload.update(findings=evidence.findings, ok=not failed, boundary=IN_MISSION_BOUNDARY)
        _emit_json(payload)
    else:
        _print_findings(evidence)
        for node in chain.writes:
            source = f"source {node.source_ref} → " if node.source_ref else ""
            console.print(
                escape(
                    f"{source}{node.op} {node.seeded_by_mutation_ref} "
                    f"(run {node.writer_run or '-'}, agent {node.writer_agent}, {node.at}) "
                    f"→ read by {len(node.read_by_runs)} run(s): "
                    f"{', '.join(node.read_by_runs) or '-'}"
                )
            )
        if chain.unresolved_reads:
            console.print(f"{chain.unresolved_reads} read(s) resolved to no recorded write.")
        console.print(IN_MISSION_BOUNDARY)
    raise typer.Exit(1 if failed else 0)
