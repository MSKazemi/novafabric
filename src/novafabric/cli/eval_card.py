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

"""``nova eval card`` and ``nova eval score`` sub-commands (NF-002/NF-010, ADR-0099).

Experimental. Surfaces the evidence-grade evaluation substrate:

    nova eval card new --source code --card-id exact-match --name "Exact Match" --out card.json
    nova eval card sign card.json                     # → signature block + digest (local keyring)
    nova eval card register card.json                 # → into the eval-card registry (signed-gated)
    nova eval card show  <card_id>@<version>          # → card JSON + digest
    nova eval card verify <card_id>@<version>         # → signature_ok / calibration → exit code

    nova eval score add  --card <card_id>@<v> --subject <sha256:…> --value 0.82 \
                         --value-type numeric --source judge --name faithfulness \
                         --scores-file scores.jsonl
    nova eval score list --scores-file scores.jsonl [--source judge] [--json]

Sealing a Score into a Run Capsule attestation (``--reseal``) is a separate slice —
these commands operate on the eval-card registry and a plain ``scores.jsonl`` file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from novafabric.eval.card import (
    Calibration,
    CardVerification,
    EvalCard,
    JudgeModel,
    card_digest,
    sign_card,
    verify_card,
)
from novafabric.eval.registry import (
    EvalCardError,
    EvalCardNotFoundError,
    asset_ref,
    card_exists,
    get_card,
    register_card,
)
from novafabric.eval.scores import (
    SCORES_FILENAME,
    Score,
    ScoreSource,
    ScoreValueType,
    append_score,
    read_scores,
)
from novafabric.trust.keyring import ensure_keypair

console = Console()

card_app = typer.Typer(name="card", help="Manage signed eval cards.", no_args_is_help=True)
score_app = typer.Typer(
    name="score", help="Record and list evidence-grade scores.", no_args_is_help=True
)


def _parse_ref(ref: str) -> tuple[str, str]:
    if "@" not in ref:
        raise typer.BadParameter(
            f"Invalid card ref {ref!r}: expected card_id@version", param_hint="ref"
        )
    card_id, version = ref.rsplit("@", 1)
    return card_id, version


def _resolve_scores_path(scores_file: Path | None, capsule: Path | None) -> Path:
    """Resolve where scores.jsonl lives.

    A ``--capsule`` dir writes ``<capsule>/scores.jsonl`` so the score is covered by
    the capsule Merkle root and sealed into any Evidence Bundle built from that capsule
    (NF-002 req 10) — the existing seal path hashes every capsule file, no seal change
    needed. ``--scores-file`` targets a standalone file instead.
    """
    if capsule is not None:
        return capsule / SCORES_FILENAME
    if scores_file is not None:
        return scores_file
    raise typer.BadParameter("provide --capsule <dir> or --scores-file <path>")


def _coerce_value(raw: str, value_type: ScoreValueType) -> bool | float | str:
    if value_type is ScoreValueType.NUMERIC:
        try:
            return float(raw)
        except ValueError as exc:
            raise typer.BadParameter(f"--value {raw!r} is not numeric") from exc
    if value_type is ScoreValueType.BOOLEAN:
        low = raw.strip().lower()
        if low in ("true", "1", "yes", "pass"):
            return True
        if low in ("false", "0", "no", "fail"):
            return False
        raise typer.BadParameter(f"--value {raw!r} is not a boolean")
    return raw


# ── nova eval card ───────────────────────────────────────────────────────────


@card_app.command("new")
def card_new(
    source: Annotated[ScoreSource, typer.Option(help="Evaluator kind.")],
    card_id: Annotated[str, typer.Option("--card-id", help="Stable evaluator id.")],
    name: Annotated[str, typer.Option(help="Human-readable name.")],
    version: Annotated[str, typer.Option(help="Semantic version.")] = "0.1.0",
    judge_model: Annotated[str | None, typer.Option(help="Judge model name.")] = None,
    endpoint_ref: Annotated[
        str, typer.Option(help="Judge endpoint reference (no hardcoded URL).")
    ] = "env:NOVA_JUDGE_ENDPOINT",
    prompt_version: Annotated[str | None, typer.Option(help="Judge prompt version.")] = None,
    rubric: Annotated[str | None, typer.Option(help="Judge rubric text.")] = None,
    dataset: Annotated[str | None, typer.Option(help="Dataset version / ref.")] = None,
    human_agreement: Annotated[float | None, typer.Option(help="Judge agreement.")] = None,
    n: Annotated[int, typer.Option(help="Judge calibration sample size.")] = 0,
    metric: Annotated[str, typer.Option(help="Judge calibration metric.")] = "cohen_kappa",
    out: Annotated[Path | None, typer.Option(help="Card JSON out (default stdout).")] = None,
) -> None:
    """Create a new (unsigned) eval card."""
    judge = None
    calibration = None
    if source is ScoreSource.JUDGE:
        if judge_model is None:
            raise typer.BadParameter("judge cards require --judge-model")
        judge = JudgeModel(name=judge_model, endpoint_ref=endpoint_ref)
        if human_agreement is not None and n > 0:
            calibration = Calibration(human_agreement=human_agreement, n=n, metric=metric)
    try:
        card = EvalCard(
            card_id=card_id,
            name=name,
            version=version,
            source=source,
            judge_model=judge,
            prompt_version=prompt_version,
            rubric=rubric,
            dataset_version=dataset,
            calibration=calibration,
        )
    except ValueError as exc:
        console.print(f"[red]Invalid card:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    payload = card.model_dump_json(exclude_none=True, indent=2)
    if out is None:
        console.print_json(payload)
    else:
        out.write_text(payload + "\n", encoding="utf-8")
        console.print(f"[green]Wrote[/green] {out}  digest={card_digest(card)}")


@card_app.command("sign")
def card_sign(
    card_file: Annotated[Path, typer.Argument(help="Path to a card JSON file.")],
    identity: Annotated[str | None, typer.Option(help="Keyring identity (default: local).")] = None,
    out: Annotated[Path | None, typer.Option(help="Signed card out (default: overwrite).")] = None,
) -> None:
    """Sign a card with the local Ed25519 keyring and print its digest."""
    card = EvalCard.model_validate_json(card_file.read_text(encoding="utf-8"))
    private_key, fingerprint = ensure_keypair(identity)
    signed = sign_card(card, private_key, key_id=fingerprint)
    target = out or card_file
    target.write_text(signed.model_dump_json(exclude_none=True, indent=2) + "\n", encoding="utf-8")
    console.print(
        f"[green]Signed[/green] {target}  key_id={fingerprint}  digest={card_digest(signed)}"
    )


@card_app.command("register")
def card_register(
    card_file: Annotated[Path, typer.Argument(help="Path to a signed card JSON file.")],
) -> None:
    """Register a signed card into the eval-card registry."""
    card = EvalCard.model_validate_json(card_file.read_text(encoding="utf-8"))
    try:
        digest = register_card(card)
    except EvalCardError as exc:
        console.print(f"[red]Registration refused:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Registered[/green] {asset_ref(card)}\n  digest={digest}")


@card_app.command("show")
def card_show(
    ref: Annotated[str, typer.Argument(help="card_id@version")],
) -> None:
    """Print a registered card and its digest."""
    card_id, version = _parse_ref(ref)
    try:
        card = get_card(card_id, version)
    except EvalCardNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    console.print_json(card.model_dump_json(exclude_none=True))
    console.print(f"digest={card_digest(card)}")


@card_app.command("verify")
def card_verify(
    ref: Annotated[str, typer.Argument(help="card_id@version")],
    identity: Annotated[str | None, typer.Option(help="Keyring identity to verify with.")] = None,
) -> None:
    """Verify a registered card's signature and calibration (non-zero on failure)."""
    card_id, version = _parse_ref(ref)
    try:
        card = get_card(card_id, version)
    except EvalCardNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    private_key, fingerprint = ensure_keypair(identity)
    if card.signature is not None and card.signature.key_id != fingerprint:
        console.print(
            f"[yellow]key_id mismatch[/yellow]: card signed by {card.signature.key_id}, "
            f"local key is {fingerprint} — cannot verify locally"
        )
        raise typer.Exit(code=2)
    result: CardVerification = verify_card(card, private_key.public_key())
    console.print(
        f"signature_ok={result.signature_ok}  calibration_present={result.calibration_present}  "
        f"digest={result.digest}"
    )
    if not result.ok:
        raise typer.Exit(code=1)


# ── nova eval score ──────────────────────────────────────────────────────────


@score_app.command("add")
def score_add(
    card: Annotated[str, typer.Option(help="Eval card ref card_id@version.")],
    subject: Annotated[str, typer.Option(help="sha256:<hex> of the scored span/capsule.")],
    value: Annotated[str, typer.Option(help="Score value (coerced per --value-type).")],
    scores_file: Annotated[
        Path | None, typer.Option("--scores-file", help="Path to scores.jsonl.")
    ] = None,
    capsule: Annotated[
        Path | None, typer.Option(help="Capsule dir; writes <capsule>/scores.jsonl (sealed).")
    ] = None,
    value_type: Annotated[
        ScoreValueType, typer.Option(help="boolean|categorical|numeric.")
    ] = ScoreValueType.NUMERIC,
    source: Annotated[
        ScoreSource, typer.Option(help="human|heuristic|code|judge.")
    ] = ScoreSource.JUDGE,
    name: Annotated[str, typer.Option(help="Metric name.")] = "score",
    subject_kind: Annotated[str, typer.Option(help="span|capsule.")] = "span",
    run_id: Annotated[str | None, typer.Option(help="Optional run/capsule ULID.")] = None,
) -> None:
    """Append an evidence-grade Score to a scores.jsonl file.

    Writing into a ``--capsule`` dir seals the score into any Evidence Bundle built from
    that capsule (it is covered by the capsule Merkle root). Refuses if the card ref does
    not resolve to a registered eval card (req 2).
    """
    target = _resolve_scores_path(scores_file, capsule)
    card_id, version = _parse_ref(card)
    try:
        eval_card = get_card(card_id, version)
    except EvalCardNotFoundError as exc:
        console.print(f"[red]{exc}[/red] — register it first with `nova eval card register`")
        raise typer.Exit(code=1) from exc
    digest = card_digest(eval_card)
    if not card_exists(digest):
        console.print(f"[red]eval card digest {digest} not registered[/red]")
        raise typer.Exit(code=1)
    try:
        score = Score(
            subject=subject,
            subject_kind=subject_kind,
            name=name,
            value=_coerce_value(value, value_type),
            value_type=value_type,
            source=source,
            evaluator_id=eval_card.card_id,
            eval_card_digest=digest,
            run_id=run_id,
        )
    except ValueError as exc:
        console.print(f"[red]Invalid score:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    append_score(target, score)
    console.print(
        f"[green]Recorded[/green] {score.score_id}  {name}={score.value}  → {target}"
    )


@score_app.command("list")
def score_list(
    scores_file: Annotated[
        Path | None, typer.Option("--scores-file", help="Path to scores.jsonl.")
    ] = None,
    capsule: Annotated[
        Path | None, typer.Option(help="Capsule dir; reads <capsule>/scores.jsonl.")
    ] = None,
    source: Annotated[ScoreSource | None, typer.Option(help="Filter by source.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")] = False,
) -> None:
    """List Scores in a scores.jsonl file (or a capsule's score log)."""
    scores = read_scores(_resolve_scores_path(scores_file, capsule))
    if source is not None:
        scores = [s for s in scores if s.source is source]
    if as_json:
        payload = [json.loads(s.model_dump_json(exclude_none=True)) for s in scores]
        console.print_json(json.dumps(payload))
        return
    table = Table(title=f"scores ({len(scores)})")
    for col in ("score_id", "name", "value", "type", "source", "eval_card_digest"):
        table.add_column(col, overflow="fold")
    for s in scores:
        table.add_row(
            s.score_id, s.name, str(s.value), s.value_type.value, s.source.value, s.eval_card_digest
        )
    console.print(table)
