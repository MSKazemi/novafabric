"""nova toolschema — tool-schema supply-chain evidence (ADR-0148 D2, experimental).

Read-only. ``nova toolschema impact`` re-validates the historical captured tool-call payloads in a
document against a **new** schema and reports exactly which runs would break under it (with per-run
failing paths). It reuses the shipped ADR-0128 validator — it does not reimplement validation.

Impact analysis is **evidence, not a gate**: the command exits ``0`` whether or not any run breaks
(``2`` only on bad input), so it can run in CI without blocking.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

app = typer.Typer(
    name="toolschema",
    help="Tool-schema replay-impact analysis over historical payloads (experimental, ADR-0148).",
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)


@app.command("impact")
def impact(
    document: Annotated[
        Path,
        typer.Argument(help="JSON: {tool_id, tool_calls:[{run_id, arguments}]}."),
    ],
    new_schema: Annotated[
        Path,
        typer.Option("--new-schema", help="Path to the new JSON Schema to test past runs against."),
    ],
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the schema_impact report as JSON."),
    ] = False,
) -> None:
    """Report which historical runs break under a new tool schema (reuses the ADR-0128 validator).

    \b
    Examples:
      nova toolschema impact calls.json --new-schema new.json
      nova toolschema impact calls.json --new-schema new.json --json
    """
    from novafabric.supplychain.toolschema.impact import compute_schema_impact

    if not document.exists():
        err_console.print(f"[red]Document not found:[/red] {document}")
        raise typer.Exit(2)
    try:
        doc = json.loads(document.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        err_console.print(f"[red]Could not read document:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("tool_calls"), list):
        err_console.print("[red]Document must be an object with a 'tool_calls' list.[/red]")
        raise typer.Exit(2)

    try:
        report = compute_schema_impact(
            tool_id=str(doc.get("tool_id", "")),
            new_schema_path=new_schema,
            tool_calls=doc["tool_calls"],
        )
    except (ValueError, TypeError) as exc:
        err_console.print(f"[red]Invalid impact input:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    n_broken = len(report.broken_run_ids)
    head = (
        f"[red]{n_broken} run(s) break[/red]" if n_broken else "[green]no runs break[/green]"
    )
    console.print(
        f"Tool-schema impact for {report.tool_id or '(unnamed tool)'} — {head} "
        f"of {report.checked} checked (evidence, not a gate)"
    )
    for b in report.broken_run_ids:
        console.print(f"  {b.run_id}: {', '.join(b.failing_paths)}")

    raise typer.Exit(0)


@app.command("track")
def track(
    tool_id: Annotated[
        str,
        typer.Option("--tool", help="Stable tool identity, e.g. mcp://acme/search."),
    ],
    from_schema: Annotated[
        Path,
        typer.Option("--from-schema", help="The previous JSON Schema."),
    ],
    to_schema: Annotated[
        Path,
        typer.Option("--to-schema", help="The new JSON Schema."),
    ],
    max_diff: Annotated[
        int,
        typer.Option("--max-diff", help="Bound on the recorded diff; the class ignores it."),
    ] = 100,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the tool_schema_change record as JSON."),
    ] = False,
) -> None:
    """Classify a tool-schema change: additive, breaking, deprecation, or unknown.

    Follows the ADR-0148 additive-safe rule — a new *optional* property is additive, while a
    removal, a type change, an optional-to-required tightening, or a **new required** property is
    breaking. A schema built from ``allOf``/``anyOf``/``oneOf``/``not``/``$ref`` is reported as
    ``unknown`` with a reason rather than guessed at, because calling an unanalysed schema
    additive would read as "safe to ship".

    It **classifies; it does not gate**: ``breaking`` is a fact about two schemas, not a decision
    about shipping one, so the command exits ``0`` whatever the class (``2`` only on bad input).
    Pair it with ``nova toolschema impact`` to see which past runs the change would break.

    \b
    Examples:
      nova toolschema track --tool mcp://acme/search --from-schema v1.json --to-schema v2.json
      nova toolschema track --tool mcp://acme/search --from-schema v1.json \\
        --to-schema v2.json --json
    """
    from novafabric.supplychain.toolschema.change import (  # noqa: PLC0415
        ToolSchemaChangeError,
        classify_schema_change,
    )

    loaded: dict[str, object] = {}
    for label, path in (("--from-schema", from_schema), ("--to-schema", to_schema)):
        if not path.exists():
            err_console.print(f"[red]Schema not found ({label}):[/red] {path}")
            raise typer.Exit(2)
        try:
            loaded[label] = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            err_console.print(f"[red]Could not read {label}:[/red] {exc}")
            raise typer.Exit(2) from exc

    try:
        change = classify_schema_change(
            tool_id=tool_id,
            from_schema=loaded["--from-schema"],
            to_schema=loaded["--to-schema"],
            max_diff_entries=max_diff,
        )
    except ToolSchemaChangeError as exc:
        err_console.print(f"[red]Could not classify:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(change.model_dump(exclude_none=True), indent=2))
        raise typer.Exit(0)

    colour = {
        "breaking": "red",
        "deprecation": "yellow",
        "unknown": "yellow",
        "additive": "green",
    }[change.change_class]
    console.print(
        f"Tool-schema change — {change.tool_id}: "
        f"[{colour}]{change.change_class}[/{colour}]\n"
        f"  {change.from_schema_digest[:14]}… -> {change.to_schema_digest[:14]}…"
    )
    for entry in change.diff:
        detail = f" ({entry.was} -> {entry.now})" if entry.was or entry.now else ""
        console.print(f"    {entry.op}: {entry.path}{detail}")
    if change.diff_truncated:
        console.print(
            f"  [dim]showing {len(change.diff)} of {change.diff_total} difference(s); "
            "the class is computed over all of them[/dim]"
        )
    if change.reason:
        console.print(f"  [dim]{change.reason}[/dim]")

    raise typer.Exit(0)


@app.command("deprecations")
def deprecations(
    capsules: Annotated[
        Path,
        typer.Option("--capsules", help="Directory of sealed Run Capsules to scan."),
    ],
    tool_id: Annotated[
        str,
        typer.Option("--tool", help="The tool whose version is retired."),
    ],
    version: Annotated[
        str,
        typer.Option("--version", help="The retired version."),
    ],
    deprecated_at: Annotated[
        str,
        typer.Option("--deprecated-at", help="ISO-8601 date the version was retired."),
    ],
    successor: Annotated[
        str | None,
        typer.Option("--successor", help="Replacement tool, when one was declared."),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit the tool_deprecation record as JSON."),
    ] = False,
) -> None:
    """Flag every sealed run still pinned to a retired tool version.

    Reads ``tool_version`` from each capsule's ``tool-calls.jsonl`` — a **required** field of the
    ADR-0128 tool-call schema.

    Three buckets, not two. ``tool_version`` is documented as *"Semver if known; 'unknown'
    otherwise"*, so a run recorded that way can be neither confirmed as pinned nor cleared: it is
    reported separately rather than folded into either answer. ``capsules_scanned`` travels with
    the result, because without it an empty dependent list cannot be told from an empty search.

    It **reports; it does not gate** — the command exits ``0`` whether or not any run is pinned
    (``2`` only on bad input).

    \b
    Examples:
      nova toolschema deprecations --capsules ./capsules --tool mcp://acme/search \\
        --version 1.0.0 --deprecated-at 2026-07-28
    """
    from novafabric.supplychain.toolschema.deprecation import (  # noqa: PLC0415
        ToolDeprecationError,
        build_deprecation,
    )

    try:
        record = build_deprecation(
            capsules,
            tool_id=tool_id,
            deprecated_version=version,
            deprecated_at=deprecated_at,
            successor=successor,
        )
    except ToolDeprecationError as exc:
        err_console.print(f"[red]Could not build the deprecation record:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(record.model_dump(exclude_none=True), indent=2))
        raise typer.Exit(0)

    console.print(
        f"Tool deprecation — {record.tool_id} @ {record.deprecated_version} "
        f"(retired {record.deprecated_at})"
        + (f", successor {record.successor}" if record.successor else "")
    )
    console.print(
        f"  {len(record.dependent_run_ids)} run(s) pinned to it, "
        f"of {record.capsules_scanned} capsule(s) scanned"
    )
    for run_id in record.dependent_run_ids:
        console.print(f"    [red]pinned[/red] {run_id}")
    for run_id in record.unknown_version_run_ids:
        console.print(f"    [yellow]unknown version[/yellow] {run_id}")
    if record.unknown_version_run_ids:
        console.print(
            "  [dim]a recorded version of 'unknown' can be neither confirmed nor cleared[/dim]"
        )

    raise typer.Exit(0)


@app.command("conformance")
def conformance(
    capsule: Annotated[
        Path,
        typer.Option("--capsule", help="Path to one sealed Run Capsule directory."),
    ],
    predicate: Annotated[
        bool,
        typer.Option("--predicate", help="Emit the in-toto predicate fragment instead."),
    ] = False,
    verify: Annotated[
        str | None,
        typer.Option("--verify", help="Check the recorded verdicts against a sealed digest."),
    ] = None,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="Emit as JSON."),
    ] = False,
) -> None:
    """Seal a capsule's recorded ADR-0128 conformance verdicts, or verify a seal.

    Reuses the recorded ``schema_validation`` verdicts verbatim — nothing is re-validated here.
    The verdicts belong in the capsule facet and the **digest alone** in the signed attestation
    (``--predicate``), which is what leaves ``--verify`` two independent sources to compare: a
    check that recomputed from the object carrying the digest could never fail.

    A call that declared no schema is counted as ``unchecked``, never as conforming — otherwise a
    capsule where nothing declared a schema would report perfect conformance.

    Sealing exits ``0`` whatever the counts (it records, it does not gate); ``--verify`` exits
    ``1`` on a mismatch, because a broken seal is a finding. ``2`` is bad input.

    \b
    Examples:
      nova toolschema conformance --capsule ./capsules/run-42 --json
      nova toolschema conformance --capsule ./capsules/run-42 --predicate --json
      nova toolschema conformance --capsule ./capsules/run-42 --verify sha256:abc…
    """
    from novafabric.drift.collect import CollectError, read_trajectory  # noqa: PLC0415
    from novafabric.supplychain.toolschema.conformance_seal import (  # noqa: PLC0415
        ConformanceSealError,
        into_predicate,
        seal_conformance,
        verify_seal,
    )

    if not capsule.is_dir():
        err_console.print(f"[red]Capsule not found:[/red] {capsule}")
        raise typer.Exit(2)
    try:
        calls = read_trajectory(capsule)
        seal = seal_conformance([c["schema_validation"] for c in calls])
    except (CollectError, ConformanceSealError) as exc:
        err_console.print(f"[red]Could not seal conformance:[/red] {exc}")
        raise typer.Exit(2) from exc

    if verify is not None:
        ok = verify_seal(seal.verdicts, verify)
        if json_out:
            print(json.dumps({"verified": ok, "recomputed": seal.sealed_digest}, indent=2))
        elif ok:
            console.print(f"[green]Seal verified[/green] — {seal.sealed_digest}")
        else:
            console.print(
                f"[red]Seal MISMATCH[/red]\n  sealed:     {verify}\n"
                f"  recomputed: {seal.sealed_digest}"
            )
        raise typer.Exit(0 if ok else 1)

    payload = into_predicate(seal) if predicate else seal.model_dump(exclude_none=True)
    if json_out:
        print(json.dumps(payload, indent=2))
        raise typer.Exit(0)

    console.print(
        f"Output conformance — {capsule.name}\n"
        f"  {seal.conforming} conforming, {seal.violating} violating, "
        f"{seal.unchecked} unchecked of {seal.calls} call(s)\n"
        f"  sealed_digest = {seal.sealed_digest}"
    )
    if seal.unchecked:
        console.print("  [dim]unchecked calls declared no schema; they are not conforming[/dim]")
    raise typer.Exit(0)
