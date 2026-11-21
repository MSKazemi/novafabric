from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape

from novafabric._paths import default_replays_dir
from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.replay._engine import ReplayEngine
from novafabric.replay._errors import ReplayOverrideUnenforceableError
from novafabric.replay._fidelity import REQUIRED_CHECKS
from novafabric.replay._fidelity import VERDICT_FAILED as FIDELITY_FAILED
from novafabric.replay._fidelity import VERDICT_FAITHFUL as FIDELITY_FAITHFUL
from novafabric.replay._flags import ReplayFlags
from novafabric.replay._hermetic import HermeticUnavailableError
from novafabric.replay._intervention import InterventionError
from novafabric.replay._servability import describe_by_system
from novafabric.replay.environment_gate import (
    EXIT_ENVIRONMENT_MISMATCH,
    ReplayEnvironmentMismatchError,
    validate_environment_value,
)


class ReplayMode(str, Enum):
    mocked = "mocked"
    forensic = "forensic"
    semantic = "semantic"
    exact = "exact"
    intervention = "intervention"

console = Console()


def replay_cmd(
    capsule: Annotated[
        Path,
        typer.Argument(
            help="Run capsule directory, or a bare run id (printed by `nova capture`, or "
            "written by a framework adapter)."
        ),
    ],
    mode: Annotated[
        ReplayMode,
        typer.Option("--mode", help="Replay mode.")
    ] = ReplayMode.mocked,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Report what would execute without running")
    ] = False,
    allow_readonly: Annotated[
        bool,
        typer.Option(
            "--allow-readonly",
            help=(
                "Safety ladder (ADR-0012): permit read-only tools. In mocked mode, "
                "under --permissive an unmatched record.tool call of that class runs "
                "live, and a replay.yaml `allow: true` override for such a tool is "
                "honoured (ADR-0306, experimental). MCP calls always count as "
                "unknown mutation."
            ),
        ),
    ] = False,
    allow_mutating: Annotated[
        bool,
        typer.Option(
            "--allow-mutating",
            help=(
                "Safety ladder (ADR-0012): permit writes/deletes (implies "
                "--allow-readonly); the replay must also pass the policy engine's "
                "replay_mutating check. In mocked mode, under --permissive an "
                "unmatched record.tool call of that class runs live, and a "
                "replay.yaml `allow: true` override for such a tool is honoured "
                "(ADR-0306, experimental)."
            ),
        ),
    ] = False,
    allow_external_side_effects: Annotated[
        bool,
        typer.Option(
            "--allow-external-side-effects",
            help=(
                "Safety ladder (ADR-0012): permit external side effects (implies "
                "--allow-mutating, including its replay_mutating policy check). In "
                "mocked mode, under --permissive an unmatched "
                "record.tool call of that class runs live, and a replay.yaml "
                "`allow: true` override for such a tool is honoured (ADR-0306, "
                "experimental)."
            ),
        ),
    ] = False,
    allow_unknown_mutation: Annotated[
        bool,
        typer.Option(
            "--allow-unknown-mutation",
            help=(
                "Safety ladder (ADR-0012): permit tools of unknown mutation class "
                "(implies every lower rung, including the replay_mutating policy "
                "check). Every MCP call counts as unknown: in "
                "mocked mode this is the flag that lets an unmatched MCP call run "
                "live under --permissive, and that a replay.yaml `allow: true` "
                "override on an MCP tool needs before it re-executes (ADR-0306, "
                "experimental)."
            ),
        ),
    ] = False,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            "-o",
            help="Base dir for replay output (default: $NOVAFABRIC_HOME/replays)",
        ),
    ] = None,
    intervention_file: Annotated[
        Path | None,
        typer.Option(
            "--intervention-file",
            help=(
                "InterventionSpec YAML for --mode intervention "
                "(experimental, ADR-0086)"
            ),
        ),
    ] = None,
    environment: Annotated[
        str | None,
        typer.Option(
            "--environment",
            help=(
                "Experimental (ADR-0126): only replay a capsule that recorded this "
                "deployment_environment (e.g. staging). Exit 2, before anything "
                "runs, if it recorded another value or none."
            ),
        ),
    ] = None,
    permissive: Annotated[
        bool,
        typer.Option(
            "--permissive",
            help=(
                "mocked mode only (ADR-0300): do NOT fail on divergence. A model "
                "call with no recorded response gets an empty reply, unsupported "
                "model surfaces run LIVE, an unmatched intercepted tool call runs "
                "live only if an --allow-* flag permits its mutation class (MCP: "
                "--allow-unknown-mutation; record.tool, Google ADK, OpenAI Agents "
                "and LangChain tools: their declared class, LangChain and Agents "
                "tools not passed through wrap_tools: unknown), never "
                "if replay.yaml says `allow: false`, and unconsumed recordings are "
                "only reported. Default is fail-closed."
            ),
        ),
    ] = False,
    require_faithful: Annotated[
        bool,
        typer.Option(
            "--require-faithful",
            help=(
                "mocked mode only, experimental (ADR-0321): exit 4 unless the replay's "
                "fidelity verdict is `faithful` -- the replay reproduced the recorded "
                "exit code, stdout (UUIDs masked), the files it wrote and the call "
                "sequence, with no divergence and no live call -- and exit 0 when it "
                "is, even for a faithful replay of a run "
                "that failed. A replay refused before it ran keeps its refusal code."
            ),
        ),
    ] = False,
    hermetic: Annotated[
        bool,
        typer.Option(
            "--hermetic",
            help=(
                "mocked mode only, experimental, Linux (ADR-0322): run the replayed "
                "process tree in a bubblewrap sandbox -- no network beyond its own "
                "loopback (host services included), host filesystem read-only except "
                "the working directory and the replay result directory, host socket "
                "dirs masked; served model/tool calls still work. Refused with "
                "exit 5 (error.type HermeticUnavailable), before anything runs, if "
                "bwrap is missing or cannot create namespaces here; never run "
                "un-isolated."
            ),
        ),
    ] = False,
) -> None:
    """Re-run a captured run against recorded or mocked LLM responses.

    Five modes control how outbound calls are handled:
      mocked       — re-runs the command (Python workloads). Serves recorded
                     responses for OpenAI chat.completions and responses, and
                     Anthropic messages calls (sync or async, streamed or not),
                     and recorded results for MCP ClientSession.call_tool,
                     for functions declared with
                     novafabric.capture.record.tool, for Google ADK tools
                     run with the NovaFabric tool plugin, for OpenAI
                     Agents function tools and for LangChain tools passed
                     through their wrap_tools (experimental).
                     Fail-closed: an extra, unmatched or unsupported call, or an
                     unconsumed recording, fails the replay (--permissive to
                     only report). replay.yaml tool_overrides are enforced
                     on every tool surface: `allow: false` is never run live,
                     `allow: true` re-executes only with the --allow-* flag
                     for its class (experimental). Other tools (HTTP,
                     shell, files, other framework-native tools, undeclared
                     functions) are NOT intercepted: they run live; outbound
                     connections are reported, not blocked
      forensic     — read-only: inspects the capsule, runs nothing
      semantic     — does not re-run: scores how similar the recorded LLM
                     responses are to each other (0.0-1.0)
      exact        — does not re-run: reports whether a byte-exact re-run is
                     possible (deterministic env.lock, seeds, no schema drift)
      intervention — experimental: substitute one captured event per an
                     InterventionSpec, re-execute downstream under mocked
                     semantics, emit a diffable counterfactual capsule (a
                     substituted tool result is not delivered: tools run live)

    Capsules written by a framework adapter or novafabric.sdk.agent
    (capture_mode: sdk-decorator) or imported from OpenTelemetry spans record
    no command to re-run: mocked refuses them up front (error.type
    CapsuleNotReplayable, exit 1, and the same under --dry-run); intervention
    emits the counterfactual streams without re-running; exact reports them
    not eligible; forensic and semantic work as usual.

    A capsule that records model calls mocked replay cannot serve (Ollama,
    Hugging Face, other providers' raw HTTP, or a served surface whose response
    was never recorded) is refused up front too (error.type
    ModelSurfaceNotServable, exit 1, also under --dry-run, which lists each as
    [LIVE] model call); --permissive runs it and reports model_calls_not_servable.
    LangChain tools and OpenAI Agents function tools NOT passed through
    wrap_tools are recorded by nova capture and served like wrapped ones
    declared unknown when the capsule holds their
    results (capture level forensic); a capsule whose recorded ones cannot be
    served is refused up front (error.type ToolSurfaceNotServed, exit 1, also
    under --dry-run, which lists each as [NOT SERVED] ... tool call), and
    one with no matching record is refused at the call; --permissive runs them
    live only with --allow-unknown-mutation, and every tool body that runs live
    is counted in tool_calls_live and named in a [LIVE] tool call line
    (experimental).
    Network is observed, not blocked, in every Python process of the replayed
    tree (children that drop the replay's environment, such as MCP stdio
    servers, are observed too); a child that is not Python is counted
    unobserved (replay_contract.child_processes_unobserved) and the network
    count is then a lower bound (network_observation_coverage: partial).
    --hermetic blocks the network instead (experimental, Linux).
    A mocked replay also reports a fidelity verdict (experimental, ADR-0321):
    faithful, diverged, failed or not_compared, against the recorded exit
    code, stdout digest (RFC 4122 UUIDs masked, rule set volatile-ids/1; raw
    stdout/stderr digests advisory), the files the recorded run wrote and
    the call sequence. The status (and the exit
    code) still says how the replayed process ended; --require-faithful gates
    on the verdict instead. The replay directory holds a replay capsule:
    `nova diff <capsule> <replay-dir>` compares the two runs.

    \b
    Exit codes:
      0  replay succeeded, or a --dry-run the real run would not refuse
      1  replay failed or was aborted (incl. CapsuleNotReplayable,
         ModelSurfaceNotServable, ToolSurfaceNotServed, a
         divergence under the fail-closed default, a replayed command that
         could not be launched: error.type ReplayLaunchError)
      2  --environment did not match the capsule's recorded environment, or
         a file the replay needs (capsule.yaml, replay.yaml, env.lock, a call
         stream) is present but unusable (a link, FIFO, directory): nothing ran
      3  mocked: refused to start (also under --dry-run) because a replay.yaml
         `allow: false` override names a tool recorded on a transport replay
         cannot intercept (error.type ToolOverrideUnenforceable); --permissive
         starts anyway and reports override_unenforceable
      4  --require-faithful: the fidelity verdict is not `faithful` (it
         replaces the code the replay would otherwise exit with)
      5  --hermetic: the sandbox cannot be set up here (error.type
         HermeticUnavailable, also under --dry-run); nothing ran
      124 mocked/intervention: the replayed command did not finish within 600s
          and was killed (error.type ReplayTimeout, printed and recorded in the
          replay result); a command that itself exits 124 has no error.type
      N  mocked/intervention: the replayed command's own non-zero exit code

    Scope: single capsule.

    \b
    Examples:
      # By run id — the id `nova capture` printed
      nova replay 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # Or by path
      nova replay path/to/my-capsule/

      # Forensic mode — observe without writing
      nova replay --mode forensic 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # Dry-run: show what would execute
      nova replay --dry-run path/to/my-capsule/

      # Mocked, but only report divergences instead of failing
      nova replay --permissive 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # No network for the whole replayed process tree (Linux, bubblewrap)
      nova replay --hermetic 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # CI gate: exit 0 only if the replay reproduced the recorded run
      nova replay --require-faithful 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # CI gate: only replay capsules recorded in staging (exit 2 otherwise)
      nova replay --environment staging --dry-run 01HXAY7M5JZ8R7K4P9DPBYK2WX

      # Counterfactual: what if the model had answered differently?
      nova replay --mode intervention --intervention-file spec.yaml path/to/my-capsule/
    """
    if environment is not None:
        try:
            validate_environment_value(environment)
        except ValueError as exc:
            raise typer.BadParameter(str(exc), param_hint="'--environment'") from exc
    try:
        capsule = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if mode is ReplayMode.intervention and intervention_file is None:
        console.print(
            "[red]--mode intervention requires --intervention-file[/red]"
        )
        raise typer.Exit(code=1)
    if intervention_file is not None and mode is not ReplayMode.intervention:
        console.print(
            "[red]--intervention-file only applies to --mode intervention[/red]"
        )
        raise typer.Exit(code=1)
    if permissive and mode is not ReplayMode.mocked:
        console.print("[red]--permissive only applies to --mode mocked[/red]")
        raise typer.Exit(code=1)
    if require_faithful and mode is not ReplayMode.mocked:
        console.print("[red]--require-faithful only applies to --mode mocked[/red]")
        raise typer.Exit(code=1)
    if hermetic and mode is not ReplayMode.mocked:
        console.print("[red]--hermetic only applies to --mode mocked[/red]")
        raise typer.Exit(code=1)

    flags = ReplayFlags(
        mode=mode.value,
        dry_run=dry_run,
        allow_readonly=allow_readonly,
        allow_mutating=allow_mutating,
        allow_external_side_effects=allow_external_side_effects,
        allow_unknown_mutation=allow_unknown_mutation,
        output_dir=output_dir,
        intervention_file=intervention_file,
        required_environment=environment,
        permissive=permissive,
        hermetic=hermetic,
    )

    base = output_dir or default_replays_dir()
    engine = ReplayEngine(capsule_dir=capsule, flags=flags, base_dir=base)
    try:
        result = engine.run()
    except ReplayEnvironmentMismatchError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_ENVIRONMENT_MISMATCH) from exc
    except InterventionError as exc:
        console.print(f"[red]✗ Intervention error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    if result.status == "dry_run":
        dry_report_path = base / result.replay_id / "dry_run_report.txt"
        if dry_report_path.exists():
            console.print(dry_report_path.read_text(), end="")
        if result.error is not None:
            # The real run would refuse this capsule: fail the dry run too.
            raise typer.Exit(code=_refusal_exit_code(result.error))
        return

    status_icon = "[green]✓[/green]" if result.status == "success" else "[red]✗[/red]"
    result_path = base / result.replay_id
    console.print(
        f"{status_icon} Replay written: {result_path}  "
        f"(replay_id={result.replay_id}  mode={result.mode})"
    )

    if result.mode == "exact" and result.exact_eligible is not None:
        if result.exact_eligible:
            console.print(
                f"  eligible for exact replay ({result.exact_hash_count or 0} "
                "model request(s) with inputs)"
            )
        else:
            console.print("  [red]not eligible for exact replay:[/red]")
            for reason in result.exact_reasons or []:
                console.print(f"    - {escape(reason)}")
    if result.mode == "semantic":
        if result.similarity_score is not None:
            console.print(f"  similarity: {result.similarity_score:.4f}")
        # a missing score is explained by the divergence line below

    if result.model_calls_available is not None:
        # ADR-0300: what was actually served, so "✓" never reads as more.
        errors = int((result.replay_contract or {}).get("model_errors_replayed") or 0)
        served_line = (
            f"  model calls: {result.model_calls_mocked} of "
            f"{result.model_calls_available} served from the capsule"
            + (f" ({errors} raised as the recorded error{'s' if errors != 1 else ''})"
               if errors else "")
            + f", {result.model_calls_unmatched or 0} unmatched"
        )
        unserved = int(result.model_calls_not_servable or 0)
        if unserved:
            # ADR-0319: never "0 of 0" while a recorded call could not be served.
            by_system = (result.replay_contract or {}).get(
                "model_calls_not_servable_by_system"
            ) or {}
            recorded = int(result.model_calls_available or 0) + unserved
            served_line = (
                f"  model calls: {result.model_calls_mocked} of {recorded} recorded "
                "served from the capsule"
                + (f" ({errors} raised as the recorded error{'s' if errors != 1 else ''})"
                   if errors else "")
                + f", {result.model_calls_unmatched or 0} unmatched; {unserved} NOT "
                f"servable ({describe_by_system(by_system)}) — not intercepted, live "
                "if the workload made it"
            )
            console.print(served_line, style="yellow", markup=False, highlight=False)
        else:
            console.print(served_line)
        not_intercepted = (result.tool_calls_recorded or 0) - (
            result.tool_calls_available or 0
        )
        console.print(
            "  tool calls (MCP call_tool, record.tool, ADK, Agents SDK, LangChain): "
            f"{result.tool_calls_mocked} of "
            f"{result.tool_calls_available or 0} served, "
            f"{result.tool_calls_live or 0} live, "
            f"{result.tool_calls_unmatched or 0} unmatched; "
            f"{not_intercepted} recorded on surfaces replay does not intercept"
        )
        contract = result.replay_contract or {}
        echoed = (contract.get("tool_calls_mocked_by_source") or {}).get("model-request-echo")
        if echoed:
            # ADR-0306 r3: results the record did not keep, recovered from the
            # next recorded model request under the recorded digest.
            console.print(
                f"  {echoed} of the served tool results came from the next recorded "
                "model request (matched to the recorded result digest)"
            )
        # Name every tool call that ran live, as the dry run names each [LIVE] call.
        for entry in contract.get("tool_calls_live_by_tool") or []:
            if not isinstance(entry, dict):
                continue
            count = int(entry.get("count") or 0)
            console.print(
                f"  [LIVE] tool call ({entry.get('surface')}: {entry.get('tool_name')})"
                + (f" x{count}" if count > 1 else "")
                + " — not served from the capsule",
                style="yellow",
                markup=False,
                highlight=False,
            )
        not_listed = int(contract.get("tool_calls_live_by_tool_not_listed") or 0)
        if not_listed:
            console.print(
                f"  [LIVE] … and {not_listed} more tool(s) not listed",
                style="yellow", markup=False, highlight=False,
            )
        echo = contract.get("tool_result_echo")
        if isinstance(echo, dict) and any(
            echo.get(k) for k in ("matched", "mismatched", "not_checked")
        ):
            # ADR-0306 D10: undeclared functions' results, as sent back to the model.
            console.print(
                f"  tool-result echoes (undeclared functions): {echo.get('matched', 0)} "
                f"matched, {echo.get('mismatched', 0)} mismatched (report-only), "
                f"{echo.get('not_checked', 0)} not checked",
                markup=False,
            )
        _print_network(contract)
        if result.hermetic is not None:
            _print_hermetic(result.hermetic)
    if result.error and (
        result.status == "aborted"
        or result.error.get("type") in _STOP_REASONS_THE_EXIT_CODE_CANNOT_SHOW
    ):
        console.print(
            f"  {result.error.get('type', 'error')}: {result.error.get('message', '')}",
            style="red",
            markup=False,
        )
    if result.divergence_reason:
        colour = "red" if result.status == "failure" else "yellow"
        console.print(f"  [{colour}]divergence: {escape(result.divergence_reason)}[/{colour}]")

    if result.env_warnings:
        for w in result.env_warnings:
            console.print(f"  [yellow]⚠ {w.get('message', '')}[/yellow]")

    if result.fidelity is not None:
        _print_fidelity(result.fidelity)

    if result.intervention is not None and not result.intervention.get(
        "substitution_delivered_to_workload", True
    ):
        console.print(
            "  [yellow]⚠ substitution not delivered to the re-executed workload: "
            f"{result.intervention.get('substitution_note', '')}[/yellow]",
            highlight=False,
        )

    if result.status == "aborted" and result.error:
        raise typer.Exit(code=_refusal_exit_code(result.error))
    if require_faithful:
        verdict = (result.fidelity or {}).get("verdict")
        if verdict != FIDELITY_FAITHFUL:
            console.print(
                f"  --require-faithful: fidelity verdict is {verdict or 'absent'} (exit "
                f"{EXIT_NOT_FAITHFUL})",
                style="red",
                markup=False,
            )
            raise typer.Exit(code=EXIT_NOT_FAITHFUL)
        return
    if result.status not in ("success", "dry_run"):
        raise typer.Exit(code=result.exit_code or 1)


#: Exit code of ``--require-faithful`` when the verdict is not ``faithful`` (ADR-0321).
EXIT_NOT_FAITHFUL = 4


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _print_network(contract: dict[str, Any]) -> None:
    """The network line and, when children were started, what was observed of them.

    ADR-0304: observed, never blocked. ADR-0319 (#52): the count covers the
    processes that reported in; a child that did not is named as unobserved and the
    count is then a lower bound -- never a bare "0".
    """
    children = int(contract.get("child_processes_spawned") or 0)
    observed = int(contract.get("child_processes_network_observed") or 0)
    unobserved = int(contract.get("child_processes_unobserved", children) or 0)
    if contract.get("network_observed"):
        live = int(contract.get("network_connections_live") or 0)
        where = ", ".join(contract.get("network_destinations") or [])
        line = (
            f"  network: {live}{'+' if contract.get('network_connections_capped') else ''}"
            f" live connection{'s' if live != 1 else ''} from the replayed Python process"
            + (f" and {_plural(observed, 'observed child process', 'observed child processes')}"
               if observed else "")
            + (f" ({where})" if where else "")
            + " — observed, not blocked"
        )
        if unobserved or contract.get("child_processes_capped"):
            line += "; a lower bound (network_observation_coverage: partial)"
        console.print(line, markup=False, highlight=False)
    if not children:
        return
    names = ", ".join(contract.get("child_processes_argv0") or []) or "unnamed"
    started = (
        f"{children}{'+' if contract.get('child_processes_capped') else ''} child "
        f"process{'es' if children != 1 else ''} started during the replay ({names})"
    )
    if unobserved:
        console.print(
            f"  ⚠ {started}; {unobserved} not observed: "
            f"{'their' if unobserved != 1 else 'its'} network activity is unknown, so the "
            "network count above is a lower bound (non-Python programs, or Python started "
            "without the replay's environment); file activity is not observed "
            "(network_observation_coverage: partial)",
            style="yellow", markup=False, highlight=False,
        )
    else:
        console.print(
            f"  {started}; {'all reported their' if children != 1 else 'it reported its'} "
            "connections (counted above); file activity is not observed",
            markup=False, highlight=False,
        )


def _print_hermetic(hermetic: dict[str, Any]) -> None:
    """ADR-0322: one line saying the run was sandboxed, and what was not measured."""
    version = hermetic.get("bwrap_version")
    console.print(
        f"  hermetic: network {hermetic.get('network')} beyond the sandbox's own loopback, "
        f"filesystem {hermetic.get('filesystem')} ({hermetic.get('mechanism')}"
        + (f" {version}" if version else "")
        + "); blocked attempts are not counted from outside the process "
        f"(counting: {hermetic.get('counting')})",
        markup=False, highlight=False,
    )


def _print_fidelity(fidelity: dict[str, Any]) -> None:
    """One line: the verdict, the checks that differ and those not compared."""
    verdict = str(fidelity.get("verdict"))
    checks = [c for c in fidelity.get("checks") or [] if isinstance(c, dict)]
    differs = [
        str(c.get("name")) for c in checks if c.get("equal") is False and not c.get("advisory")
    ]
    advisory = [str(c.get("name")) for c in checks if c.get("equal") is False and c.get("advisory")]
    unchecked = [str(n) for n in fidelity.get("unchecked") or []]
    uncompared = [
        str(c.get("name")) for c in checks
        if c.get("equal") is None and c.get("name") in REQUIRED_CHECKS
        and c.get("name") not in unchecked
    ]
    line = f"  fidelity: {verdict}"
    if differs:
        line += f" — differs: {', '.join(differs)}"
    if uncompared:
        line += f" — not compared: {', '.join(uncompared)}"
    if advisory:
        line += f" — also differs (advisory, not in the verdict): {', '.join(advisory)}"
    if verdict == FIDELITY_FAILED and fidelity.get("reason"):
        line += f" — {fidelity['reason']}"
    colour = "green" if verdict == FIDELITY_FAITHFUL else "yellow"
    console.print(line, style=colour, markup=False, highlight=False)
    if "file_effects" in unchecked:
        console.print(
            "    file effects were not recorded for this capsule, so the verdict does not "
            "cover files the run wrote",
            style="yellow", markup=False, highlight=False,
        )
    for name in (n for n in unchecked if n != "file_effects"):
        console.print(
            f"    {name} was not recorded for this capsule; the verdict does not cover it",
            style="yellow", markup=False, highlight=False,
        )
    for check in checks:  # a file the replay changed: name it (first 5)
        diffs = check.get("differences") if check.get("equal") is False else None
        if isinstance(diffs, list) and diffs:
            paths = [str(d.get("path")) for d in diffs[:5] if isinstance(d, dict)]
            more = f" (+{len(diffs) - len(paths)} more)" if len(diffs) > len(paths) else ""
            text = (
                f"written only by the replay (advisory, not in the verdict): {', '.join(paths)}"
                if check.get("advisory")
                else f"{check.get('name')}: {', '.join(paths)}"
                + " differ from the recording"
            )
            console.print(f"    {text}{more}", style=colour, markup=False, highlight=False)


#: A replay that stopped for one of these exits with a code the replayed command could
#: also return itself (124 on a timeout, 1 on a launch error), so the reason is printed.
_STOP_REASONS_THE_EXIT_CODE_CANNOT_SHOW = frozenset({"ReplayTimeout", "ReplayLaunchError"})


def _refusal_exit_code(error: dict[str, Any]) -> int:
    """Exit status of a replay refused before anything ran."""
    if error.get("type") == ReplayOverrideUnenforceableError.error_type:
        return ReplayOverrideUnenforceableError.exit_code
    if error.get("type") == HermeticUnavailableError.error_type:
        return HermeticUnavailableError.exit_code
    return 1
