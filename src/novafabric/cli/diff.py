from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.eval.regression_diff import (
    DEFAULT_ALPHA,
    DEFAULT_BETA,
    DEFAULT_P0,
    DEFAULT_P1,
    significance_diff,
)
from novafabric.eval.scores import SCORES_FILENAME, ScoreValueType, read_scores
from novafabric.registry.service import AssetNotFoundError, get_asset


class DiffOutputFormat(str, Enum):
    text = "text"
    json = "json"
    github_annotation = "github-annotation"

console = Console()


def _plain(text: str) -> None:
    """Print recorded values verbatim: no markup, and no Rich highlighting.

    Group labels and capsule paths are recorded or user-chosen strings. With
    colour on (FORCE_COLOR, a TTY) Rich's highlighter split them with escape
    sequences at every bracket and digit, so ``(no variant)`` was not the text
    the terminal received.
    """
    console.print(text, markup=False, highlight=False)


def _flatten(d: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            result.update(_flatten(v, key))
        else:
            result[key] = v
    return result


#: Group label for a capsule that recorded no ADR-0116 variant block.
_NO_VARIANT_GROUP = "(no variant)"
#: Group label for a capsule that recorded no (valid) ADR-0126 deployment_environment.
_NO_ENVIRONMENT_GROUP = "(no environment)"
#: Recorded dimensions ``--group-by`` can partition a capsule diff by.
GROUP_BY_DIMENSIONS: tuple[str, ...] = ("variant", "environment")
#: Exit code of ``--assert-no-regressions`` (and ``--assert-same-shape``) when the
#: comparison was made and found a difference. It means nothing else (ADR-0303).
EXIT_DIFFERENCES = 1
#: Exit code when the comparison cannot be made: a capsule ref that does not
#: resolve, a capsule.yaml/env.lock that cannot be read (ADR-0303 Am. 2), an
#: asset ref not in the registry, or a usage error. A CI gate must be
#: able to tell "the runs differ" from "there was nothing to compare" (ADR-0303).
EXIT_CANNOT_COMPARE = 2
#: Exit code when ``--environment`` excludes a capsule: the requested comparison
#: cannot be made, which is not the same as "changes found" (exit 1).
EXIT_ENVIRONMENT_MISMATCH = EXIT_CANNOT_COMPARE


def _recorded_environment(capsule: Path) -> str | None:
    """ADR-0126 typed ``deployment_environment`` recorded in ``capsule.yaml``.

    Read-only and record-only: the same reader the policy input uses, so a
    missing, malformed, or rule-violating value is ``None`` — never inferred.
    """
    from novafabric.policy._environment import (  # noqa: PLC0415
        deployment_environment_from_capsule,
    )

    return deployment_environment_from_capsule(capsule)


def _environment_group(capsule: Path) -> str:
    """Group key for ``--group-by environment``: the recorded value or a placeholder."""
    return _recorded_environment(capsule) or _NO_ENVIRONMENT_GROUP


def _validate_environment_filter(environment: str) -> str:
    """Reject a ``--environment`` value no capsule could ever have recorded."""
    from novafabric.capture.deployment_env import ENVIRONMENT_VALUE_PATTERN  # noqa: PLC0415

    if not ENVIRONMENT_VALUE_PATTERN.match(environment):
        raise typer.BadParameter(
            f"invalid environment {environment!r}: must match "
            f"{ENVIRONMENT_VALUE_PATTERN.pattern} (ADR-0126 value rule)",
            param_hint="'--environment'",
        )
    return environment


def _environment_exclusions(
    environment: str, capsules: tuple[Path, Path]
) -> list[tuple[Path, str | None]]:
    """Capsules (with what they recorded) that did NOT record ``environment``."""
    excluded: list[tuple[Path, str | None]] = []
    for capsule in capsules:
        recorded = _recorded_environment(capsule)
        if recorded != environment:
            excluded.append((capsule, recorded))
    return excluded


def _variant_group(capsule: Path) -> str:
    """Read-only ADR-0116 group key ``experiment_id/variant_id`` from capsule.yaml.

    Operates purely on recorded attribution — never assigns, defaults, or
    mutates anything. A capsule without a ``variant`` block (or with a
    malformed one) groups under ``(no variant)``.
    """
    import yaml

    try:
        manifest = yaml.safe_load((capsule / "capsule.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return _NO_VARIANT_GROUP
    block = manifest.get("variant") if isinstance(manifest, dict) else None
    if not isinstance(block, dict):
        return _NO_VARIANT_GROUP
    experiment_id = block.get("experiment_id")
    variant_id = block.get("variant_id")
    if not isinstance(experiment_id, str) or not isinstance(variant_id, str):
        return _NO_VARIANT_GROUP
    return f"{experiment_id}/{variant_id}"


def _capsule_diff(
    capsule_a: Path,
    capsule_b: Path,
    output_format: DiffOutputFormat,
    assert_no_regressions: bool,
    group_by: str | None = None,
    graph_shape: bool = False,
    assert_same_shape: bool = False,
    environment: str | None = None,
) -> None:
    from novafabric.diff._engine import CapsuleFileError, DiffEngine
    from novafabric.diff._format import (
        format_github_annotations,
        format_json,
        format_text,
        malformed_line_messages,
    )

    resolved: list[Path] = []
    for p in (capsule_a, capsule_b):
        try:
            resolved.append(resolve_capsule_ref(p))
        except CapsuleRefError as exc:
            # markup=False: the message quotes the user's ref, which may contain "[".
            console.print(str(exc), style="red", markup=False)
            raise typer.Exit(code=EXIT_CANNOT_COMPARE) from exc
    capsule_a, capsule_b = resolved

    # ADR-0126 P2: --environment admits only capsules that recorded that value.
    # Fail closed — a capsule with no recorded environment is excluded too.
    if environment is not None:
        excluded = _environment_exclusions(environment, (capsule_a, capsule_b))
        for capsule, recorded in excluded:
            shown = repr(recorded) if recorded is not None else "no deployment_environment"
            typer.echo(
                f"--environment {environment}: {capsule} recorded {shown}; "
                "both capsules must have recorded it",
                err=True,
            )
        if excluded:
            raise typer.Exit(code=EXIT_ENVIRONMENT_MISMATCH)

    # ADR-0116 / ADR-0126: read-only grouping by a recorded dimension.
    groups: dict[str, str] | None = None
    if group_by == "variant":
        groups = {
            str(capsule_a): _variant_group(capsule_a),
            str(capsule_b): _variant_group(capsule_b),
        }
    elif group_by == "environment":
        groups = {
            str(capsule_a): _environment_group(capsule_a),
            str(capsule_b): _environment_group(capsule_b),
        }
    groups_key, cross_key = (
        ("environment_groups", "cross_environment")
        if group_by == "environment"
        else ("variant_groups", "cross_arm")
    )

    try:
        report = DiffEngine().compare(capsule_a, capsule_b)
    except CapsuleFileError as exc:
        # ADR-0303 Am. 2: a capsule whose manifest or env.lock cannot be read is
        # "cannot compare" (2) in every format, gate or not — the same as a ref
        # that does not resolve. stderr, so stdout carries no partial report.
        typer.echo(
            f"cannot compare: {exc}. Repair or re-capture the capsule (exit 2).",
            err=True,
        )
        raise typer.Exit(code=EXIT_CANNOT_COMPARE) from exc
    # A skipped record line is never silent (ADR-0303 Am. 1): stderr in every
    # output format, so it reaches a CI log without corrupting JSON on stdout.
    for message in malformed_line_messages(report):
        typer.echo(f"warning: {message}", err=True)

    # ADR-0124 P3: opt-in agent-graph shape pre-check. Absent both flags, nothing
    # below changes — the default output stays byte-identical.
    shape = None
    if graph_shape or assert_same_shape:
        from novafabric.diff.graph_shape import (
            compare_graph_shapes,
            malformed_source_messages,
        )

        shape = compare_graph_shapes(capsule_a, capsule_b)
        # Skipped graph-source lines are never silent either (ADR-0303 Am. 2).
        for message in malformed_source_messages(shape):
            typer.echo(f"warning: graph shape: {message}", err=True)

    if output_format == "json":
        # Machine-readable output must bypass Rich: console.print soft-wraps at
        # terminal width, inserting newlines inside long JSON string values
        # (e.g. absolute capsule paths) and corrupting the document.
        if groups is not None:
            payload: dict[str, Any] = {
                groups_key: groups,
                cross_key: len(set(groups.values())) > 1,
                "diff": report.as_dict(),
            }
            if shape is not None:
                payload["graph_shape"] = shape.to_document()
            if environment is not None:
                payload["environment_filter"] = environment
            typer.echo(json.dumps(payload, indent=2))
        elif shape is not None or environment is not None:
            doc = report.as_dict()
            if shape is not None:
                doc["graph_shape"] = shape.to_document()
            if environment is not None:
                doc["environment_filter"] = environment
            typer.echo(json.dumps(doc, indent=2))
        else:
            typer.echo(format_json(report))
    elif output_format == "github-annotation":
        typer.echo(format_github_annotations(report))
        if shape is not None:
            from novafabric.diff.graph_shape import format_graph_shape_annotations

            typer.echo("\n".join(format_graph_shape_annotations(shape)))
    else:
        if environment is not None:
            _plain(f"Environment filter: both capsules recorded {environment} (ADR-0126)")
        if groups is not None:
            group_a, group_b = groups[str(capsule_a)], groups[str(capsule_b)]
            if group_by == "environment":
                title = "Environment groups (ADR-0126, recorded deployment_environment):"
                within, cross = "Within-environment diff", "Cross-environment diff"
            else:
                title = "Variant groups (ADR-0116, recorded attribution):"
                within, cross = "Within-arm diff", "Cross-arm diff"
            _plain(title)
            _plain(f"  {group_a}: {capsule_a}")
            _plain(f"  {group_b}: {capsule_b}")
            if group_a == group_b:
                _plain(f"{within} (both capsules in group {group_a}):")
            else:
                _plain(f"{cross}: {group_a} → {group_b}")
            console.print("")
        # markup=False: output paths are workload-chosen file names, and Rich read
        # ``outputs/[bold]x.txt`` as markup and printed ``outputs/x.txt``.
        console.print(format_text(report), markup=False, highlight=False)
        if shape is not None:
            from novafabric.diff.graph_shape import format_graph_shape_text

            console.print("")
            console.print(format_graph_shape_text(shape), markup=False, highlight=False)

    if assert_no_regressions:
        # Checked before has_changes: over an incomplete read neither "the runs
        # differ" nor "they do not" is established — a skipped line can be the
        # record that pairs with an added/removed entry (ADR-0303 Am. 1).
        if not report.is_complete:
            typer.echo(
                f"--assert-no-regressions: cannot compare: {report.malformed_line_count} "
                "malformed record line(s) were skipped, so the runs were not fully "
                "compared (exit 2). Repair or re-capture the capsule.",
                err=True,
            )
            raise typer.Exit(code=EXIT_CANNOT_COMPARE)
        if report.has_changes:
            raise typer.Exit(code=EXIT_DIFFERENCES)
    if assert_same_shape and shape is not None:
        # Checked before the verdict, as for --assert-no-regressions: a shape
        # built over partial records establishes neither "same" nor "changed"
        # (ADR-0303 Am. 2).
        if shape.status != "unavailable" and not shape.is_complete:
            typer.echo(
                "--assert-same-shape: cannot compare: malformed graph-source lines were "
                "skipped, so the shapes cover only the records that parsed (exit 2). "
                "Repair or re-capture the capsule.",
                err=True,
            )
            raise typer.Exit(code=EXIT_CANNOT_COMPARE)
        if not shape.same_shape:
            # Fail closed: 1 = shapes differ; 2 = a graph could not be built, so
            # "same shape" cannot be verified.
            raise typer.Exit(
                code=EXIT_CANNOT_COMPARE if shape.status == "unavailable" else EXIT_DIFFERENCES
            )


def _resolve_scores(path: Path) -> Path:
    return path / SCORES_FILENAME if path.is_dir() else path


def _read_outcomes(path: Path, metric: str) -> list[int]:
    """Read a boolean metric from a scores.jsonl (or capsule dir) as a 0/1 sequence."""
    scores = [s for s in read_scores(_resolve_scores(path)) if s.name == metric]
    if not scores:
        raise typer.BadParameter(f"no scores named {metric!r} in {path}")
    outcomes: list[int] = []
    for s in scores:
        if s.value_type is not ScoreValueType.BOOLEAN:
            raise typer.BadParameter(
                f"metric {metric!r} must be a boolean pass/fail score for --significance"
            )
        outcomes.append(1 if s.value else 0)
    return outcomes


def _run_significance(
    baseline: Path | None,
    candidate: Path | None,
    metric: str,
    p0: float,
    p1: float,
    alpha: float,
    beta: float,
    as_json: bool,
) -> None:
    """NF-007: statistically-grounded regression diff over stored scores (zero-token)."""
    if baseline is None or candidate is None:
        raise typer.BadParameter("--significance requires --baseline and --candidate")
    base = _read_outcomes(baseline, metric)
    cand = _read_outcomes(candidate, metric)
    try:
        diff = significance_diff(base, cand, metric=metric, p0=p0, p1=p1, alpha=alpha, beta=beta)
    except ValueError as exc:  # invalid p0/p1/alpha/beta from the SPRT primitive
        raise typer.BadParameter(str(exc)) from exc
    if as_json:
        console.print_json(diff.model_dump_json())
    else:
        bw = diff.baseline.wilson
        cw = diff.candidate.wilson
        console.print(f"metric: {metric}")
        console.print(f"baseline:  {diff.baseline.successes}/{diff.baseline.n}  "
                      f"wilson=[{bw[0]:.3f}, {bw[1]:.3f}]")
        console.print(f"candidate: {diff.candidate.successes}/{diff.candidate.n}  "
                      f"wilson=[{cw[0]:.3f}, {cw[1]:.3f}]")
        color = "red" if diff.is_regression() else "green"
        console.print(
            f"SPRT verdict: [{color}]{diff.sprt.verdict.value}[/{color}]  llr={diff.sprt.llr:.2f}"
        )
    code = diff.exit_code()
    if code != 0:
        raise typer.Exit(code=code)


def diff_cmd(
    ref_a: Annotated[str | None, typer.Argument(help="name@version  or  path/to/capsule-a")] = None,
    ref_b: Annotated[str | None, typer.Argument(help="name@version  or  path/to/capsule-b")] = None,
    output_format: Annotated[
        DiffOutputFormat,
        typer.Option(
            "--output-format",
            help=(
                "Output format: text, json or github-annotation. Applies to capsule "
                "and name@version asset diffs alike."
            ),
        )
    ] = DiffOutputFormat.text,
    assert_no_regressions: Annotated[
        bool, typer.Option("--assert-no-regressions", help="Exit 1 if any changes detected")
    ] = False,
    group_by: Annotated[
        str | None,
        typer.Option(
            "--group-by",
            help=(
                "Group the capsules under comparison by a recorded dimension "
                "before diffing: 'variant' (ADR-0116, recorded experiment_id/"
                "variant_id; labels cross-arm or within-arm) or 'environment' "
                "(ADR-0126, experimental; recorded deployment_environment; labels "
                "cross-environment or within-environment). Read-only; capsule "
                "paths only; text/json output only."
            ),
        ),
    ] = None,
    environment: Annotated[
        str | None,
        typer.Option(
            "--environment",
            help=(
                "Experimental (ADR-0126): only compare capsules that recorded this "
                "deployment_environment (e.g. production). Exit 2 if either capsule "
                "recorded another value or none. Capsule paths only."
            ),
        ),
    ] = None,
    significance: Annotated[
        bool, typer.Option("--significance", help="Statistical regression diff (NF-007).")
    ] = False,
    baseline: Annotated[
        Path | None, typer.Option(help="Baseline scores.jsonl or capsule dir (--significance).")
    ] = None,
    candidate: Annotated[
        Path | None, typer.Option(help="Candidate scores.jsonl or capsule dir (--significance).")
    ] = None,
    metric: Annotated[str, typer.Option(help="Boolean metric name for the gate.")] = "task_pass",
    p0: Annotated[float, typer.Option(help="Acceptable pass-rate H0.")] = DEFAULT_P0,
    p1: Annotated[float, typer.Option(help="Regression-threshold pass-rate H1.")] = DEFAULT_P1,
    alpha: Annotated[float, typer.Option(help="False-positive budget.")] = DEFAULT_ALPHA,
    beta: Annotated[float, typer.Option(help="False-negative budget.")] = DEFAULT_BETA,
    sig_json: Annotated[bool, typer.Option("--json", help="Emit the diff record as JSON.")] = False,
    media: Annotated[
        bool,
        typer.Option("--media", help="Compare the two capsules' media parts (NF-170)."),
    ] = False,
    perceptual: Annotated[
        bool,
        typer.Option("--perceptual", help="Also compare media by pHash (--media; opt-in)."),
    ] = False,
    hamming_threshold: Annotated[
        int,
        typer.Option("--hamming", help="Near-duplicate distance for --perceptual."),
    ] = 10,
    graph_shape: Annotated[
        bool,
        typer.Option(
            "--graph-shape",
            help=(
                "Experimental (ADR-0124): add a graph_shape block — rebuild both "
                "capsules' agent execution graphs and report 'same shape' or the "
                "node/edge deltas. Capsule diffs only."
            ),
        ),
    ] = False,
    assert_same_shape: Annotated[
        bool,
        typer.Option(
            "--assert-same-shape",
            help=(
                "Experimental (ADR-0124): implies --graph-shape; exit 1 if the "
                "agent-graph shapes differ, 2 if either graph is unavailable or "
                "its reconstruction skipped a malformed source line."
            ),
        ),
    ] = False,
) -> None:
    """Compare two asset versions or two run capsules.

    Accepts either asset refs (name@version) or capsule directory paths.
    Both arguments must be the same type — two asset refs or two capsule paths.

    Scope: two assets or two capsules.

    \b
    Examples:
      # Compare two capsule directories
      nova diff runs/run-01/ runs/run-02/

      # Compare two registered asset versions
      nova diff my-agent@v1.0 my-agent@v1.1

      # Statistical regression diff over stored scores (exit 3 on a significant regression)
      nova diff --significance --baseline base/ --candidate cand/ --metric task_pass

      # Group two capsules by their recorded A/B variant (ADR-0116, record-only)
      nova diff --group-by variant runs/arm-a/ runs/arm-b/

      # Production vs staging, labelled by recorded environment (ADR-0126)
      nova diff --group-by environment runs/prod-01/ runs/staging-01/

      # Only diff if both capsules were recorded in production (exit 2 otherwise)
      nova diff --environment production runs/run-01/ runs/run-02/

      # Fail CI if any difference is found
      nova diff --assert-no-regressions my-agent@v1.0 my-agent@v1.1

      # Field-level asset spec diff as JSON (keys as in `nova asset diff`)
      nova diff my-agent@v1.0 my-agent@v1.1 --output-format json

      # Shape-change pre-check over the agent execution graphs (ADR-0124);
      # exit 1 if the control-flow shape differs, 2 if a graph is unavailable
      nova diff runs/run-01/ runs/run-02/ --graph-shape
      nova diff runs/run-01/ runs/run-02/ --assert-same-shape

      # Compare the two runs' media parts by exact hash, then by pHash (NF-170)
      nova diff --media runs/run-01/ runs/run-02/
      nova diff --media --perceptual runs/run-01/ runs/run-02/ --json

    \b
    Exit codes (capsule and asset diffs):
      0  no difference, or a difference with no gate flag (the diff only reports)
      1  --assert-no-regressions found a difference -- a changed, added or
         removed entry in any section -- or --assert-same-shape found a
         shape change. 1 means nothing else.
      2  the comparison could not be made: a capsule ref that does not resolve,
         an unreadable or malformed capsule.yaml or env.lock (with or without
         a gate flag), an asset ref not in the registry, a usage error, --environment
         excluded a capsule, --assert-same-shape could not build a graph or
         read malformed graph-source lines (model-calls, tool-calls or
         trace.jsonl; checked before the shape verdict), or
         --assert-no-regressions read a capsule with malformed record lines
         (they are skipped, counted and warned about on stderr, so the
         comparison is incomplete; checked before any difference)
      3  --significance found a significant regression (SPRT accept_h1)
    --media reports and never gates: 0 whatever it finds, 2 if it cannot run.
    """
    if environment is not None:
        environment = _validate_environment_filter(environment)
        if media or significance:
            raise typer.BadParameter(
                "--environment cannot be combined with --media or --significance",
                param_hint="'--environment'",
            )
    if (media or significance) and (graph_shape or assert_same_shape):
        raise typer.BadParameter(
            "--graph-shape/--assert-same-shape cannot be combined with --media or --significance",
            param_hint="'--graph-shape'",
        )

    # NF-170 media diff — capsule paths only, and a distinct output shape.
    if media:
        _run_media_diff(ref_a, ref_b, perceptual, hamming_threshold, sig_json)
        return

    # NF-007 statistical regression diff — a distinct mode with no positional refs.
    if significance:
        _run_significance(baseline, candidate, metric, p0, p1, alpha, beta, sig_json)
        return
    if ref_a is None or ref_b is None:
        raise typer.BadParameter("provide two refs to compare, or use --significance")

    # ADR-0116: --group-by is a read-only convenience over recorded attribution.
    if group_by is not None and group_by not in GROUP_BY_DIMENSIONS:
        raise typer.BadParameter(
            f"unsupported --group-by dimension {group_by!r}: "
            f"supported: {', '.join(GROUP_BY_DIMENSIONS)}",
            param_hint="'--group-by'",
        )
    if group_by is not None and output_format == DiffOutputFormat.github_annotation:
        raise typer.BadParameter(
            "--group-by is not supported with --output-format github-annotation "
            "(use text or json)",
            param_hint="'--group-by'",
        )

    # Route to capsule diff if neither arg looks like an asset ref (name@version)
    if "@" not in ref_a or "@" not in ref_b:
        _capsule_diff(
            Path(ref_a), Path(ref_b), output_format, assert_no_regressions,
            group_by=group_by,
            graph_shape=graph_shape,
            assert_same_shape=assert_same_shape,
            environment=environment,
        )
        return

    if graph_shape or assert_same_shape:
        raise typer.BadParameter(
            "--graph-shape/--assert-same-shape apply to capsule diffs only "
            "(asset refs carry no agent execution graph)",
            param_hint="'--graph-shape'",
        )

    if group_by is not None:
        raise typer.BadParameter(
            "--group-by applies to capsule diffs only (asset refs carry no "
            "variant attribution or deployment environment)",
            param_hint="'--group-by'",
        )
    if environment is not None:
        raise typer.BadParameter(
            "--environment applies to capsule diffs only (an asset has no "
            "deployment environment)",
            param_hint="'--environment'",
        )

    _asset_diff(ref_a, ref_b, output_format, assert_no_regressions)


def _asset_diff_document(
    ref_a: str, ref_b: str, spec_a: dict[str, Any], spec_b: dict[str, Any]
) -> dict[str, Any]:
    """Field-level diff of two flattened asset specs, as one JSON document.

    The keys are those of ``nova asset diff --output-format json`` (``ref_a``,
    ``ref_b``, ``identical``, ``added``, ``removed``, ``changed``), so the same
    comparison has one shape whichever command produced it, plus ``has_changes``,
    the property ``--assert-no-regressions`` reads (ADR-0303). A key present on
    one side only is added or removed even when its value is ``null``.
    """
    added = {k: spec_b[k] for k in sorted(spec_b.keys() - spec_a.keys())}
    removed = {k: spec_a[k] for k in sorted(spec_a.keys() - spec_b.keys())}
    changed = {
        k: {"from": spec_a[k], "to": spec_b[k]}
        for k in sorted(spec_a.keys() & spec_b.keys())
        if spec_a[k] != spec_b[k]
    }
    has_changes = bool(added or removed or changed)
    return {
        "ref_a": ref_a,
        "ref_b": ref_b,
        "has_changes": has_changes,
        "identical": not has_changes,
        "added": added,
        "removed": removed,
        "changed": changed,
    }


def _asset_diff_messages(doc: dict[str, Any]) -> list[str]:
    """One line per differing spec field, in key order: added, removed, changed."""
    messages = [f"{k}: (absent) → {v!r}" for k, v in doc["added"].items()]
    messages += [f"{k}: {v!r} → (absent)" for k, v in doc["removed"].items()]
    messages += [f"{k}: {c['from']!r} → {c['to']!r}" for k, c in doc["changed"].items()]
    return messages


def _asset_diff(
    ref_a: str, ref_b: str, output_format: DiffOutputFormat, assert_no_regressions: bool
) -> None:
    """``nova diff name@version name@version``: compare two registered asset specs.

    Honours ``--output-format`` like the capsule path. ``text`` and
    ``github-annotation`` print to stdout; ``json`` prints one document
    (:func:`_asset_diff_document`) with no Rich wrapping. It always printed text,
    so a CI step asking for JSON got a document it could not parse.
    """
    from novafabric.diff._format import _annotation_data  # noqa: PLC0415

    def parse_ref(ref: str) -> tuple[str, str]:
        n, v = ref.rsplit("@", 1)
        return n, v

    name_a, version_a = parse_ref(ref_a)
    name_b, version_b = parse_ref(ref_b)

    try:
        asset_a = get_asset(name_a, version_a)
        asset_b = get_asset(name_b, version_b)
    except AssetNotFoundError as exc:
        console.print(str(exc), style="red", markup=False)
        raise typer.Exit(code=EXIT_CANNOT_COMPARE) from exc

    doc = _asset_diff_document(
        ref_a,
        ref_b,
        _flatten(json.loads(asset_a.get("spec_json") or "{}")),
        _flatten(json.loads(asset_b.get("spec_json") or "{}")),
    )
    messages = _asset_diff_messages(doc)

    if output_format == DiffOutputFormat.json:
        typer.echo(json.dumps(doc, indent=2))
    elif output_format == DiffOutputFormat.github_annotation:
        level = "error" if doc["has_changes"] else "notice"
        lines = [
            f"::{level} title=NovaFabric Diff::{_annotation_data(f'Asset spec field {m}')}"
            for m in messages
        ]
        if not doc["has_changes"]:
            lines.append("::notice title=NovaFabric Diff::No differences found.")
        typer.echo("\n".join(lines))
    elif not doc["has_changes"]:
        console.print("[green]No differences found.[/green]")
    else:
        # markup=False: spec keys and values are user-written, and Rich read a
        # "[bold]" in one as markup (and crashed on "[/x]").
        _plain(f"--- {ref_a}")
        _plain(f"+++ {ref_b}")
        console.print()
        for message in messages:
            _plain(f"  {message}")

    if assert_no_regressions and doc["has_changes"]:
        raise typer.Exit(code=EXIT_DIFFERENCES)


def _run_media_diff(
    ref_a: str | None,
    ref_b: str | None,
    perceptual: bool,
    threshold: int,
    json_out: bool,
) -> None:
    """NF-170: compare two capsules' media parts by exact hash, optionally also by pHash.

    Perceptual comparison needs an image decoder, which is **not** a declared dependency
    (ADR-0148 status note). When one is unavailable the command says so and exits ``2``, rather
    than returning exact-only results under a ``--perceptual`` flag — that would report "no
    near-duplicates" about a check that never ran.

    It reports; it does not gate: exit ``0`` whatever the classifications.
    """
    from novafabric.diff.media import (  # noqa: PLC0415
        MediaDiffError,
        diff_media,
        pillow_decoder,
    )

    if ref_a is None or ref_b is None:
        raise typer.BadParameter("--media needs two capsule paths")

    decoder = None
    if perceptual:
        try:
            pillow_decoder(b"")
        except MediaDiffError as exc:
            if "not installed" in str(exc):
                console.print(f"[red]--perceptual unavailable:[/red] {exc}")
                raise typer.Exit(2) from exc
            decoder = pillow_decoder  # a decode failure here just means the probe bytes were bad
        else:  # pragma: no cover - the empty probe never decodes successfully
            decoder = pillow_decoder

    try:
        result = diff_media(
            ref_a, ref_b, perceptual=perceptual, decoder=decoder, threshold=threshold
        )
    except MediaDiffError as exc:
        console.print(f"[red]Could not diff media:[/red] {exc}")
        raise typer.Exit(2) from exc

    if json_out:
        print(json.dumps(result.model_dump(exclude_none=True), indent=2))
        raise typer.Exit(0)

    counts = result.counts
    console.print(
        f"Media diff — {result.parts_a} part(s) vs {result.parts_b}, pairing {result.pairing}"
        + (f", perceptual (hamming <= {threshold})" if result.perceptual else "")
    )
    console.print("  " + (", ".join(f"{v}: {n}" for v, n in sorted(counts.items())) or "no parts"))
    for pair in result.pairs:
        if pair.verdict == "identical":
            continue
        detail = f" (hamming {pair.hamming})" if pair.hamming is not None else ""
        console.print(f"    [{pair.index}] {pair.verdict}{detail}")
        if pair.perceptual_unavailable:
            console.print(f"        [dim]{pair.perceptual_unavailable}[/dim]")
    raise typer.Exit(0)
