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

"""``nova experiment`` — dataset-experiment harness (experimental, ADR-0120).

Run a target command across every item of a pinned JSONL dataset (one Run
Capsule per item), record an immutable content-addressed ``Experiment``, and
compare two experiments through the existing ADR-0080 significance gate
(exit 3 on a statistically significant regression). Fully local, offline,
zero-token.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn, Optional

import typer
from rich.console import Console
from rich.table import Table

from novafabric.eval.experiment import TargetKind

if TYPE_CHECKING:
    from novafabric.eval.experiment import Experiment
    from novafabric.eval.experiment_compare import ExperimentComparison

console = Console()

experiment_app = typer.Typer(
    name="experiment",
    help="Dataset-experiment harness: run, list, show, compare (experimental, ADR-0120).",
    no_args_is_help=True,
)

_EXPERIMENTS_DIR_OPTION = typer.Option(
    "--experiments-dir",
    help="Experiment store (default: $NOVAFABRIC_EXPERIMENTS_DIR or ./.novafabric/experiments).",
)


def _fail(message: str, *, code: int = 2) -> NoReturn:
    console.print(f"[red]Experiment error:[/red] {message}")
    raise typer.Exit(code=code)


def _print_summary(experiment: Experiment) -> None:
    ok = sum(1 for r in experiment.runs if r.status.value == "ok")
    console.print(
        f"[green]✓[/green] Experiment [bold]{experiment.experiment_id}[/bold] finalized"
    )
    console.print(
        f"  dataset: {experiment.dataset_ref.name}@{experiment.dataset_ref.version} "
        f"({experiment.dataset_ref.dataset_hash[:19]}…)"
    )
    console.print(f"  items: {ok}/{len(experiment.runs)} ok")
    for agg in experiment.aggregate:
        band = ""
        if agg.wilson is not None:
            band = f"  wilson=[{agg.wilson[0]:.3f}, {agg.wilson[1]:.3f}]"
        console.print(
            f"  {agg.metric}: {agg.reducer}={agg.value:.3f}  n={agg.n}{band}"
        )


def _print_comparison(comparison: ExperimentComparison, as_json: bool) -> None:
    if as_json:
        typer.echo(comparison.model_dump_json(indent=2))
        return
    sprt = comparison.significance.get("sprt", {})
    changed = sum(1 for item in comparison.per_item if item.changed)
    unmatched = sum(
        1 for item in comparison.per_item if item.baseline is None or item.candidate is None
    )
    console.print(
        f"metric: {comparison.metric}  items: {len(comparison.per_item)} "
        f"(changed={changed}, unmatched={unmatched})"
    )
    color = "red" if comparison.is_regression() else "green"
    console.print(
        f"SPRT verdict: [{color}]{sprt.get('verdict')}[/{color}]  "
        f"llr={sprt.get('llr', 0.0):.2f}  (exit {comparison.exit_code})"
    )


@experiment_app.command(
    "run",
    context_settings={"allow_extra_args": True, "allow_interspersed_args": False},
)
def experiment_run_cmd(
    ctx: typer.Context,
    command: Annotated[
        list[str],
        typer.Argument(
            help="Command to run once per dataset item ({input}/{item_id}/{expected} "
            "placeholders are substituted)."
        ),
    ],
    dataset: Annotated[
        Path, typer.Option("--dataset", help="JSONL dataset file (one item per line).")
    ],
    target: Annotated[
        str, typer.Option("--target", help="Resolved target ref under test, e.g. my-agent@1.2.0.")
    ],
    target_kind: Annotated[
        TargetKind, typer.Option("--target-kind", help="Target kind.")
    ] = TargetKind.AGENT,
    target_label: Annotated[
        Optional[str],
        typer.Option("--target-label", help="Deployment label the ref was resolved through."),
    ] = None,
    metric: Annotated[
        str, typer.Option("--metric", help="Boolean metric name for the exact-match scorer.")
    ] = "exact_match",
    dataset_name: Annotated[
        Optional[str], typer.Option("--dataset-name", help="Dataset name (default: file stem).")
    ] = None,
    dataset_version: Annotated[
        Optional[str],
        typer.Option("--dataset-version", help="Dataset version label (default: content hash)."),
    ] = None,
    baseline: Annotated[
        Optional[str],
        typer.Option(
            "--baseline",
            help="Baseline experiment id/path: also compare and exit with the gate's code.",
        ),
    ] = None,
    runs_dir: Annotated[
        Optional[Path],
        typer.Option("--runs-dir", help="Base directory for the per-item Run Capsules."),
    ] = None,
    experiments_dir: Annotated[Optional[Path], _EXPERIMENTS_DIR_OPTION] = None,
    out: Annotated[
        Optional[Path],
        typer.Option("--out", "-o", help="Also write the finalized record to this path."),
    ] = None,
    timeout_s: Annotated[
        Optional[float], typer.Option("--timeout", help="Per-item timeout in seconds.")
    ] = None,
) -> None:
    """Run a command across every dataset item; record an immutable Experiment.

    One Run Capsule per item (existing capture path); items with an ``expected``
    value get a zero-token boolean exact-match score. With ``--baseline`` the
    run is also compared against a stored experiment and the process exits with
    the ADR-0080 gate code (3 = significant regression).

    Scope: one dataset x one target; fully local and offline.

    \b
    Examples:
      nova experiment run --dataset items.jsonl --target my-agent@1.2.0 -- \\
          python agent.py --question "{input}"

      # CI gate against a stored baseline (exit 3 on significant regression)
      nova experiment run --dataset items.jsonl --target my-agent@1.3.0 \\
          --baseline 01JZ… -- python agent.py "{input}"
    """
    from novafabric.eval.experiment import (
        ExperimentError,
        ExperimentTarget,
        load_experiment,
        save_experiment,
    )
    from novafabric.eval.experiment_compare import compare_experiments
    from novafabric.eval.experiment_dataset import DatasetError, load_dataset
    from novafabric.eval.experiment_runner import run_experiment

    full_command = list(command) + list(ctx.args)
    try:
        loaded = load_dataset(dataset, name=dataset_name, version=dataset_version)
    except DatasetError as exc:
        _fail(str(exc))

    baseline_exp = None
    if baseline is not None:
        try:
            baseline_exp = load_experiment(baseline, experiments_dir)
        except ExperimentError as exc:
            _fail(str(exc))

    experiment = run_experiment(
        loaded,
        full_command,
        target=ExperimentTarget(kind=target_kind, ref=target, label=target_label),
        metric=metric,
        runs_dir=runs_dir,
        baseline_experiment_id=baseline_exp.experiment_id if baseline_exp else None,
        timeout_s=timeout_s,
    )
    try:
        path = save_experiment(experiment, experiments_dir)
    except ExperimentError as exc:
        _fail(str(exc))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(experiment.model_dump(mode="json", exclude_none=True), indent=2) + "\n",
            encoding="utf-8",
        )
    _print_summary(experiment)
    console.print(f"  stored: {path}")

    if baseline_exp is not None:
        try:
            comparison = compare_experiments(baseline_exp, experiment, metric=metric)
        except ExperimentError as exc:
            _fail(str(exc))
        _print_comparison(comparison, as_json=False)
        if comparison.exit_code != 0:
            raise typer.Exit(code=comparison.exit_code)


@experiment_app.command("list")
def experiment_list_cmd(
    experiments_dir: Annotated[Optional[Path], _EXPERIMENTS_DIR_OPTION] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """List stored experiments (oldest first)."""
    from novafabric.eval.experiment import ExperimentError, list_experiments

    try:
        experiments = list_experiments(experiments_dir)
    except ExperimentError as exc:
        _fail(str(exc))
    if as_json:
        typer.echo(
            json.dumps(
                [e.model_dump(mode="json", exclude_none=True) for e in experiments], indent=2
            )
        )
        return
    if not experiments:
        console.print("No experiments recorded.")
        return
    table = Table("experiment_id", "dataset", "target", "items", "status", "created_at")
    for e in experiments:
        table.add_row(
            e.experiment_id,
            f"{e.dataset_ref.name}@{e.dataset_ref.version}",
            e.target.ref,
            str(len(e.runs)),
            e.status,
            e.created_at,
        )
    console.print(table)


@experiment_app.command("show")
def experiment_show_cmd(
    experiment_id: Annotated[str, typer.Argument(help="Experiment id or record path.")],
    experiments_dir: Annotated[Optional[Path], _EXPERIMENTS_DIR_OPTION] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the raw JSON record.")] = False,
) -> None:
    """Show one experiment record (per-item runs + aggregate)."""
    from novafabric.eval.experiment import ExperimentError, load_experiment

    try:
        experiment = load_experiment(experiment_id, experiments_dir)
    except ExperimentError as exc:
        _fail(str(exc))
    if as_json:
        typer.echo(json.dumps(experiment.model_dump(mode="json", exclude_none=True), indent=2))
        return
    _print_summary(experiment)
    for run in experiment.runs:
        console.print(f"  {run.item_id}: {run.status.value}  capsule={run.capsule_ref}")


@experiment_app.command("compare")
def experiment_compare_cmd(
    baseline: Annotated[str, typer.Argument(help="Baseline experiment id or record path.")],
    candidate: Annotated[str, typer.Argument(help="Candidate experiment id or record path.")],
    metric: Annotated[
        str, typer.Option("--metric", help="Metric to compare.")
    ] = "exact_match",
    p0: Annotated[float, typer.Option(help="Acceptable pass-rate H0 (ADR-0080).")] = 0.9,
    p1: Annotated[float, typer.Option(help="Regression-threshold pass-rate H1.")] = 0.7,
    alpha: Annotated[float, typer.Option(help="False-positive budget.")] = 0.05,
    beta: Annotated[float, typer.Option(help="False-negative budget.")] = 0.05,
    experiments_dir: Annotated[Optional[Path], _EXPERIMENTS_DIR_OPTION] = None,
    out: Annotated[
        Optional[Path],
        typer.Option("--out", "-o", help="Write the comparison record to this path."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit the record as JSON.")] = False,
) -> None:
    """Compare two experiments; exit 3 on a statistically significant regression.

    Per-item alignment by ``item_id``; the verdict is produced verbatim by the
    existing ADR-0080 significance gate. Comparing experiments over different
    pinned datasets is a hard error.

    \b
    Examples:
      nova experiment compare 01JX… 01JY… --metric exact_match
    """
    from novafabric.eval.experiment import ExperimentError, load_experiment
    from novafabric.eval.experiment_compare import compare_experiments

    try:
        baseline_exp = load_experiment(baseline, experiments_dir)
        candidate_exp = load_experiment(candidate, experiments_dir)
        comparison = compare_experiments(
            baseline_exp, candidate_exp, metric=metric, p0=p0, p1=p1, alpha=alpha, beta=beta
        )
    except ValueError as exc:  # invalid p0/p1/alpha/beta from the SPRT primitive
        _fail(str(exc))
    except ExperimentError as exc:
        _fail(str(exc))
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(comparison.model_dump_json(indent=2) + "\n", encoding="utf-8")
    _print_comparison(comparison, as_json)
    if comparison.exit_code != 0:
        raise typer.Exit(code=comparison.exit_code)
