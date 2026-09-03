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
) -> None:
    from novafabric.diff._engine import DiffEngine
    from novafabric.diff._format import format_github_annotations, format_json, format_text

    resolved: list[Path] = []
    for p in (capsule_a, capsule_b):
        try:
            resolved.append(resolve_capsule_ref(p))
        except CapsuleRefError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    capsule_a, capsule_b = resolved

    # ADR-0116: read-only grouping by recorded (experiment_id, variant_id).
    groups: dict[str, str] | None = None
    if group_by == "variant":
        groups = {
            str(capsule_a): _variant_group(capsule_a),
            str(capsule_b): _variant_group(capsule_b),
        }

    report = DiffEngine().compare(capsule_a, capsule_b)

    if output_format == "json":
        # Machine-readable output must bypass Rich: console.print soft-wraps at
        # terminal width, inserting newlines inside long JSON string values
        # (e.g. absolute capsule paths) and corrupting the document.
        if groups is not None:
            payload = {
                "variant_groups": groups,
                "cross_arm": len(set(groups.values())) > 1,
                "diff": report.as_dict(),
            }
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.echo(format_json(report))
    elif output_format == "github-annotation":
        typer.echo(format_github_annotations(report))
    else:
        if groups is not None:
            group_a, group_b = groups[str(capsule_a)], groups[str(capsule_b)]
            console.print("Variant groups (ADR-0116, recorded attribution):")
            console.print(f"  {group_a}: {capsule_a}")
            console.print(f"  {group_b}: {capsule_b}")
            if group_a == group_b:
                console.print(f"Within-arm diff (both capsules in group {group_a}):")
            else:
                console.print(f"Cross-arm diff: {group_a} → {group_b}")
            console.print("")
        console.print(format_text(report))

    if assert_no_regressions and report.changed_count > 0:
        raise typer.Exit(code=1)


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
        typer.Option("--output-format", help="Output format.")
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
                "before diffing. Only 'variant' is supported (ADR-0116): groups "
                "by the capsule's recorded (experiment_id, variant_id) and "
                "labels the diff as cross-arm or within-arm. Read-only; "
                "capsule paths only; text/json output only."
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

      # Fail CI if any difference is found
      nova diff --assert-no-regressions my-agent@v1.0 my-agent@v1.1

      # Compare the two runs' media parts by exact hash, then by pHash (NF-170)
      nova diff --media runs/run-01/ runs/run-02/
      nova diff --media --perceptual runs/run-01/ runs/run-02/ --json
    """
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
    if group_by is not None and group_by != "variant":
        raise typer.BadParameter(
            f"unsupported --group-by dimension {group_by!r}: only 'variant' is supported",
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
        )
        return

    if group_by is not None:
        raise typer.BadParameter(
            "--group-by applies to capsule diffs only (asset refs carry no "
            "variant attribution)",
            param_hint="'--group-by'",
        )

    def parse_ref(ref: str) -> tuple[str, str]:
        n, v = ref.rsplit("@", 1)
        return n, v

    name_a, version_a = parse_ref(ref_a)
    name_b, version_b = parse_ref(ref_b)

    try:
        asset_a = get_asset(name_a, version_a)
        asset_b = get_asset(name_b, version_b)
    except AssetNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    spec_a = _flatten(json.loads(asset_a.get("spec_json", "{}")))
    spec_b = _flatten(json.loads(asset_b.get("spec_json", "{}")))

    all_keys = set(spec_a) | set(spec_b)
    diffs = {
        k: (spec_a.get(k), spec_b.get(k))
        for k in sorted(all_keys)
        if spec_a.get(k) != spec_b.get(k)
    }

    if not diffs:
        console.print("[green]No differences found.[/green]")
        return

    console.print(f"--- {ref_a}")
    console.print(f"+++ {ref_b}")
    console.print()
    for k, (va, vb) in diffs.items():
        console.print(f"  [cyan]{k}[/cyan]: {va!r} → {vb!r}")


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
