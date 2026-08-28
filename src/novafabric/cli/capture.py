from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Optional

if TYPE_CHECKING:
    from novafabric.masking import MaskingPipeline

import typer
from rich.console import Console

from novafabric._paths import default_capsule_dir
from novafabric.capture.deployment_env import InvalidDeploymentEnvironmentError
from novafabric.capture.orchestrator import AssetStatusCheckError, CaptureOrchestrator
from novafabric.capture.session import InvalidSessionMembershipError
from novafabric.capture.variant import InvalidVariantAttributionError
from novafabric.runners import (
    UnknownRunnerError,
    get_runner,
)


class RunnerName(str, Enum):
    local = "local"
    docker = "docker"
    kubernetes = "kubernetes"
    slurm = "slurm"
    lsf = "lsf"
    pbs = "pbs"

console = Console()


def _parse_runner_option(raw: str) -> tuple[str, str]:
    if "=" not in raw:
        raise typer.BadParameter(
            f"--runner-option must be key=value, got: {raw!r}",
            param_hint="'--runner-option'",
        )
    key, _, value = raw.partition("=")
    key = key.strip()
    if not key:
        raise typer.BadParameter(
            f"--runner-option key cannot be empty: {raw!r}",
            param_hint="'--runner-option'",
        )
    return key, value


def _parse_status_list(raw: str | None) -> list[str] | None:
    """Parse a comma-separated status list into a list of stripped strings."""
    if not raw:
        return None
    return [s.strip() for s in raw.split(",") if s.strip()]


def capture_cmd(
    ctx: typer.Context,
    command: Annotated[list[str], typer.Argument(help="Command and args to capture")],
    output_dir: Annotated[
        Path | None,
        typer.Option("--output-dir", "-o", help="Base directory for capsule runs"),
    ] = None,
    runner_name: Annotated[
        RunnerName,
        typer.Option(
            "--runner",
            help="Runner backend for executing the captured command.",
        ),
    ] = RunnerName.local,
    runner_options: Annotated[
        list[str] | None,
        typer.Option(
            "--runner-option",
            help=(
                "Runner-specific option as key=value. "
                "Example: --runner-option image=myorg/agent:latest"
            ),
        ),
    ] = None,
    timeout: Annotated[
        float | None,
        typer.Option(
            "--timeout",
            help="Wall-clock deadline in seconds for the captured command (default: 600).",
        ),
    ] = None,
    # C-1.5 — asset status enforcement flags
    asset_ref: Annotated[
        Optional[str],
        typer.Option(
            "--asset",
            help=(
                "Named asset to check before capturing (e.g. my-agent@v1). "
                "Used with --require-asset-status or --warn-if-asset-status."
            ),
        ),
    ] = None,
    require_asset_status: Annotated[
        Optional[str],
        typer.Option(
            "--require-asset-status",
            help=(
                "Comma-separated list of lifecycle statuses the asset must be in "
                "before capture starts. Exits with a non-zero code if not satisfied. "
                "Example: --require-asset-status staging,production"
            ),
        ),
    ] = None,
    warn_if_asset_status: Annotated[
        Optional[str],
        typer.Option(
            "--warn-if-asset-status",
            help=(
                "Comma-separated list of lifecycle statuses that trigger a warning "
                "(but do not block capture). "
                "Example: --warn-if-asset-status development"
            ),
        ),
    ] = None,
    environment: Annotated[
        Optional[str],
        typer.Option(
            "--environment",
            help=(
                "Deployment-environment tag recorded on the capsule as "
                "deployment_environment (ADR-0126): conventionally production | "
                "staging | development | test, or any custom string (e.g. "
                "prod-eu). Recorded verbatim, never inferred. Overrides the "
                "NOVAFABRIC_ENVIRONMENT env var. Distinct from the env.lock "
                "technical environment. Omitted = absent (unknown)."
            ),
        ),
    ] = None,
    experiment: Annotated[
        Optional[str],
        typer.Option(
            "--experiment",
            help=(
                "A/B experiment id recorded verbatim in the capsule's optional "
                "variant block (ADR-0116, record-only). Requires --variant and "
                "--variant-source. NovaFabric never assigns variants."
            ),
        ),
    ] = None,
    variant: Annotated[
        Optional[str],
        typer.Option(
            "--variant",
            help=(
                "Active A/B variant (arm) id recorded verbatim in the capsule's "
                "optional variant block (ADR-0116, record-only). Requires "
                "--experiment and --variant-source; overrides the "
                "NOVAFABRIC_VARIANT* env vars. Omitted = no variant recorded."
            ),
        ),
    ] = None,
    variant_label: Annotated[
        Optional[str],
        typer.Option(
            "--variant-label",
            help="Optional human-readable arm name (e.g. control) for the variant block.",
        ),
    ] = None,
    variant_source: Annotated[
        Optional[str],
        typer.Option(
            "--variant-source",
            help=(
                "The EXTERNAL system that assigned the arm (e.g. launchdarkly, "
                "statsig, upstream-router), recorded verbatim as "
                "assignment_source. Required with --variant/--experiment — "
                "NovaFabric never allocates, so it never defaults this."
            ),
        ),
    ] = None,
    variant_assigned_at: Annotated[
        Optional[str],
        typer.Option(
            "--variant-assigned-at",
            help=(
                "Optional RFC 3339 UTC timestamp of the external assignment. "
                "Never defaulted to the capture time."
            ),
        ),
    ] = None,
    session_id: Annotated[
        Optional[str],
        typer.Option(
            "--session-id",
            help=(
                "Session (multi-turn conversation/workflow) this run belongs "
                "to, recorded as the optional session_id back-reference on the "
                "capsule (ADR-0122, experimental). Must be a ULID — create one "
                "with `nova session new`. Overrides NOVAFABRIC_SESSION_ID. "
                "Omitted = a standalone run. Not the parent/child hierarchy."
            ),
        ),
    ] = None,
    session_sequence: Annotated[
        Optional[int],
        typer.Option(
            "--session-sequence",
            help=(
                "Zero-based turn index of this run within --session-id, "
                "recorded as the optional sequence back-reference (ADR-0122). "
                "Requires --session-id; the session.json manifest stays the "
                "authoritative ordered index."
            ),
        ),
    ] = None,
    require_registered: Annotated[
        bool,
        typer.Option(
            "--require-registered",
            help=(
                "Block capture if the named asset (--asset) is not in the registry. "
                "By default, an unregistered asset produces a warning only."
            ),
        ),
    ] = False,
    mark_provenance: Annotated[
        bool,
        typer.Option(
            "--mark-provenance",
            help=(
                "Write a C2PA synthetic-content provenance marker (c2pa-manifest.json) "
                "into the capsule when the run produces model output. Satisfies the "
                "EU AI Act Art.50 machine-readable AI-generated-content disclosure. "
                "The marker is sealed with the capsule when NovaSeal is configured."
            ),
        ),
    ] = False,
    fast_emit: Annotated[
        bool,
        typer.Option(
            "--fast-emit",
            help=(
                "Install capture hooks lazily in the workload subprocess: a "
                "target SDK is patched only if/when the workload imports it, so "
                "unused SDKs are never imported at startup just to be patched. "
                "Cuts per-run startup cost #2 (ADR-0092 slice B). Fidelity is "
                "unchanged. Runs in-process (not delegated to the daemon)."
            ),
        ),
    ] = False,
    emit_spool: Annotated[
        bool,
        typer.Option(
            "--emit-spool",
            help=(
                "Also write run-boundary EventEnvelope v1 records to the local "
                "event spool ($NOVAFABRIC_SPOOL_DIR, default $NOVAFABRIC_HOME/"
                "spool) so the resident drain can forward them to the collector "
                "tier (ADR-0092 slice C). Off by default; edge stays keyless "
                "(signing happens at the hub). Runs in-process (not delegated)."
            ),
        ),
    ] = False,
    emit_otel_genai: Annotated[
        bool,
        typer.Option(
            "--emit-otel-genai",
            help=(
                "After capture, emit OTel GenAI gen_ai.* spans (OTLP-shaped JSON) "
                "alongside the capsule at <capsule>/otel-genai-spans.json (NF-032, "
                "ADR-0098). Portable, additive; message content is omitted unless "
                "--capture-content is also set."
            ),
        ),
    ] = False,
    capture_content: Annotated[
        bool,
        typer.Option(
            "--capture-content",
            help=(
                "With --emit-otel-genai, include request messages in the emitted "
                "spans, routed through the ADR-0009 secret-redaction gate and "
                "size-bounded (NF-033 opt-in content bridge). Off by default."
            ),
        ),
    ] = False,
    capture_media: Annotated[
        bool,
        typer.Option(
            "--capture-media",
            help=(
                "EXPERIMENTAL (ADR-0125): store the bytes of multimodal message "
                "parts (inline base64 images/audio/documents on model calls) "
                "content-addressed in the capsule blob store "
                "(outputs/<sha256>.<ext>), bounded per part. Off by default "
                "(ADR-0021 privacy-by-default): parts record reference metadata "
                "only — media_type, sha256 content_hash, byte_size — and the "
                "bytes are discarded after hashing."
            ),
        ),
    ] = False,
    masker: Annotated[
        list[str] | None,
        typer.Option(
            "--masker",
            help=(
                "EXPERIMENTAL (ADR-0135): enable a registered PII masker for "
                "this capture, by 'novafabric.maskers' entry-point name or "
                "dotted import path (repeatable). Maskers run AFTER the "
                "built-in secret scanner — built-ins always run — and every "
                "mask is recorded in redaction-proof.json. "
                "Example: --masker novafabric-email"
            ),
        ),
    ] = None,
    masking_config: Annotated[
        Path | None,
        typer.Option(
            "--masking-config",
            help=(
                "EXPERIMENTAL (ADR-0135): path to a masking.yaml describing "
                "the custom masking pipeline. Defaults to "
                ".novafabric/masking.yaml when that file exists. Absent or "
                "'enabled: false' ⇒ exact ADR-0009 built-in behavior."
            ),
        ),
    ] = None,
    daemon: Annotated[
        bool,
        typer.Option(
            "--daemon/--no-daemon",
            help=(
                "Delegate a plain capture to the warm capture daemon if one is "
                "running (eliminates per-run cold-start). Auto: falls back to "
                "in-process direct spawn when no daemon is reachable. Ignored when "
                "any of --runner/--runner-option/--timeout/--asset/--environment/"
                "--experiment/--variant*/--session*/--mark-provenance/--capture-media/"
                "--output-dir are set (those run in-process to honor the flags)."
            ),
        ),
    ] = True,
) -> None:
    """Wrap any shell command and record its execution as a replayable capsule.

    All LLM calls, tool use, stdin/stdout, and timing are recorded.
    The resulting capsule can be replayed, diffed, audited, or sealed.

    Scope: single run. Output written to NOVAFABRIC_HOME/capsules/.

    \b
    Examples:
      # Capture a script
      nova capture python agent.py

      # Gate on asset status before capturing
      nova capture --asset my-agent@v1 --require-asset-status staging,production -- python agent.py

      # Capture using Docker runner
      nova capture --runner docker --runner-option image=myorg/agent:latest python agent.py

      # Set a 5-minute wall-clock deadline
      nova capture --timeout 300 python agent.py

      # Tag the run with its deployment environment (ADR-0126)
      nova capture --environment production -- python agent.py

      # Record which externally assigned A/B variant was active (ADR-0116, record-only)
      nova capture --experiment exp1 --variant arm-b --variant-source statsig -- python agent.py

      # Tag the run as turn 2 of a multi-turn session (ADR-0122, experimental)
      nova capture --session-id 01HZ8S9K3M4YZ2K7N9DPBYK2W0 --session-sequence 2 -- python agent.py

      # Mark AI-generated output for EU AI Act Art.50 (writes c2pa-manifest.json)
      nova capture --mark-provenance python agent.py
    """
    # Collect args: explicit Argument list + any extra args captured via context
    cmd = list(command) + list(ctx.args)
    if not cmd:
        raise typer.BadParameter("COMMAND is required", param_hint="'COMMAND'")

    # ADR-0135 (experimental): resolve the custom masking pipeline before the
    # workload runs — registration is fail-closed, so a mis-configured masker
    # aborts capture at start rather than silently skipping expected masking.
    _default_masking_yaml = Path(".novafabric") / "masking.yaml"
    _masking_requested = (
        bool(masker)
        or masking_config is not None
        or _default_masking_yaml.is_file()
    )
    pipeline = None
    if _masking_requested:
        pipeline = _build_masking_pipeline(masker, masking_config, _default_masking_yaml)

    # Warm-daemon delegation (ADR-0092): only for a *plain* invocation. The thin
    # client forwards argv/cwd/env only, so any flag the daemon worker does not
    # carry forces in-process execution to preserve behavior/fidelity.
    _is_plain = (
        runner_name is RunnerName.local
        and not runner_options
        and timeout is None
        and asset_ref is None
        and require_asset_status is None
        and warn_if_asset_status is None
        and environment is None
        and experiment is None
        and variant is None
        and variant_label is None
        and variant_source is None
        and variant_assigned_at is None
        and session_id is None
        and session_sequence is None
        and not require_registered
        and not mark_provenance
        and not fast_emit
        and not emit_spool
        and not emit_otel_genai
        and not capture_media
        and output_dir is None
        and pipeline is None
    )
    if daemon and _is_plain:
        from novafabric._paths import daemon_socket_path

        sock_path = daemon_socket_path()
        if sock_path.exists():
            import socket as _socket

            try:
                probe = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
                probe.connect(str(sock_path))
                probe.close()
            except OSError:
                pass  # daemon not reachable → fall through to in-process spawn
            else:
                os.execvp("novacap", ["novacap", *cmd])  # never returns

    try:
        runner = get_runner(runner_name.value)
    except UnknownRunnerError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--runner'") from exc

    options_dict: dict[str, object] = {}
    for raw in runner_options or []:
        key, value = _parse_runner_option(raw)
        options_dict[key] = value

    require_statuses = _parse_status_list(require_asset_status)
    warn_statuses = _parse_status_list(warn_if_asset_status)

    # Resolve registry DB path from env (same path used by the registry CLI).
    db_env = os.environ.get("NOVAFABRIC_DB_PATH")
    registry_db_path = Path(db_env) if db_env else None

    base_dir = output_dir or default_capsule_dir()
    orch = CaptureOrchestrator(
        base_dir=base_dir,
        runner=runner,
        mark_provenance=mark_provenance,
        fast_emit=fast_emit,
        emit_spool=emit_spool,
        masking_pipeline=pipeline,
        capture_media=capture_media,
    )

    try:
        result = orch.run(
            command=cmd,
            runner_options=options_dict or None,
            timeout_s=timeout,
            asset_ref=asset_ref,
            require_asset_statuses=require_statuses,
            warn_if_asset_statuses=warn_statuses,
            require_registered=require_registered,
            registry_db_path=registry_db_path,
            environment=environment,
            experiment=experiment,
            variant=variant,
            variant_label=variant_label,
            variant_source=variant_source,
            variant_assigned_at=variant_assigned_at,
            session_id=session_id,
            session_sequence=session_sequence,
        )
    except AssetStatusCheckError as exc:
        console.print(f"[red]Asset status check failed:[/red] {exc}")
        raise typer.Exit(code=1)
    except InvalidDeploymentEnvironmentError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--environment'") from exc
    except InvalidVariantAttributionError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--variant'") from exc
    except InvalidSessionMembershipError as exc:
        raise typer.BadParameter(str(exc), param_hint="'--session-id'") from exc

    status_icon = "[green]✓[/green]" if result.exit_code == 0 else "[red]✗[/red]"
    if result.runner_status == "failed_setup":
        # The workload never ran. Exit code 127 here is NovaFabric reporting a
        # setup failure, not the workload's own status — and a real program
        # that exits 127 produces the identical code, so the exit code alone
        # cannot carry this. Only runner_status can, so key the message on it.
        detail = result.runner_error or "the runner did not report a reason"
        console.print(f"[red]✗[/red] Workload never started: {detail}")
    console.print(
        f"{status_icon} Capsule written: {result.capsule_dir}  "
        f"(run_id={result.run_id})"
    )

    # NF-032/033: opt-in OTel GenAI span emission alongside the capsule.
    if emit_otel_genai:
        import json as _json

        from novafabric.otel.genai_emitter import emit_spans

        spans = emit_spans(result.capsule_dir, capture_content=capture_content)
        spans_path = result.capsule_dir / "otel-genai-spans.json"
        spans_path.write_text(_json.dumps(spans, indent=2), encoding="utf-8")
        content_note = "with redacted content" if capture_content else "no content"
        console.print(
            f"[green]✓[/green] OTel GenAI spans: {spans_path} "
            f"({len(spans)} spans, {content_note})"
        )

    if result.exit_code == 0 and os.environ.get("NOVAFABRIC_SUGGEST", "1") != "0":
        _print_suggestion_hint(result.capsule_dir, registry_db_path)
    if result.exit_code != 0:
        raise typer.Exit(code=result.exit_code)


def _build_masking_pipeline(
    maskers: list[str] | None,
    masking_config: Path | None,
    default_path: Path,
) -> "MaskingPipeline | None":
    """Resolve the ADR-0135 masking pipeline for this capture (fail-closed).

    An explicit ``--masking-config`` wins over the discovered
    ``.novafabric/masking.yaml``. ``--masker NAME`` entries are appended with
    default bounds. A config or registration error aborts capture *before*
    the workload runs — expected masking is never silently skipped.
    """
    from novafabric.masking import (
        MaskerRegistrationError,
        MaskerSpec,
        MaskingConfigError,
        MaskingPipeline,
        load_maskers,
        load_masking_config,
    )

    config_path = masking_config
    if config_path is None and default_path.is_file():
        config_path = default_path

    try:
        specs: list[MaskerSpec] = []
        if config_path is not None:
            cfg = load_masking_config(config_path)
            if cfg.masking.enabled:
                specs.extend(cfg.masking.maskers)
        for name in maskers or []:
            specs.append(MaskerSpec(id=name))
        if not specs:
            return None
        return MaskingPipeline(load_maskers(specs))
    except (MaskingConfigError, MaskerRegistrationError) as exc:
        console.print(f"[red]Masking pipeline error (fail-closed):[/red] {exc}")
        raise typer.Exit(code=2) from exc


def _print_suggestion_hint(capsule_dir: Path, db_path: Path | None) -> None:
    """Print a brief hint if the capsule contains unregistered assets."""
    try:
        from novafabric.registry.suggestion_engine import SuggestionEngine

        engine = SuggestionEngine()
        suggestions = engine.analyze(capsule_dir, db_path=db_path)
        if not suggestions:
            return
        by_type: dict[str, int] = {}
        for s in suggestions:
            by_type[s.asset_type] = by_type.get(s.asset_type, 0) + 1
        summary = "  ".join(f"{v} {k}{'s' if v > 1 else ''}" for k, v in sorted(by_type.items()))
        console.print(f"[dim]Unregistered assets detected: {summary}[/dim]")
        console.print(f"[dim]Run `nova suggest-register {capsule_dir.name}` to review.[/dim]")
        console.print("[dim](Suppress with NOVAFABRIC_SUGGEST=0)[/dim]")
    except Exception:
        pass  # suggestion hint is best-effort; never block capture output
