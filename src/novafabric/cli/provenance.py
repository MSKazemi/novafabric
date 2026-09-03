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

"""``nova provenance`` — per-artifact content-provenance binding (ADR-0148 D1).

Five read/bind surfaces over the NF-161/162/163 facets. ``bind`` is the only one that
writes, and it writes only into the capsule manifest it was pointed at.

**Exit codes are a contract.** ``0`` means the command did its job — including a
``verify`` that found a *broken* binding, because reporting a broken binding is the job
succeeding, not failing. ``1`` is reserved for ``verify --strict``, where the caller has
explicitly asked for a gate. ``2`` is a usage or input error. Making a failed binding
exit non-zero by default would turn every reader of this command into a gate nobody
declared (the NF-156 exit-code lesson).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console

from novafabric.trust.provenance._honesty import HONESTY_LINE

app = typer.Typer(
    name="provenance",
    help=(
        "Per-artifact content provenance: bind C2PA/Content-Credentials manifests to "
        "captured media hashes (experimental, ADR-0148 D1)."
    ),
    no_args_is_help=True,
)

console = Console()
err_console = Console(stderr=True)

_MANIFEST_NAME = "capsule.yaml"


def _capsule_dir(capsule: Path) -> Path:
    """Resolve a capsule argument to its directory, erroring out if it is not one."""
    if capsule.is_dir():
        return capsule
    err_console.print(f"[red]Capsule directory not found:[/red] {capsule}")
    raise typer.Exit(2)


def _read_manifest(capsule_dir: Path) -> dict[str, Any]:
    path = capsule_dir / _MANIFEST_NAME
    if not path.exists():
        err_console.print(f"[red]{_MANIFEST_NAME} not found in[/red] {capsule_dir}")
        raise typer.Exit(2)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        err_console.print(f"[red]Could not read {_MANIFEST_NAME}:[/red] {exc}")
        raise typer.Exit(2) from exc
    if not isinstance(data, dict):
        err_console.print(f"[red]{_MANIFEST_NAME} is not a mapping.[/red]")
        raise typer.Exit(2)
    return data


def _write_manifest(capsule_dir: Path, manifest: dict[str, Any]) -> None:
    path = capsule_dir / _MANIFEST_NAME
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")


def _load_manifest_documents(paths: list[Path] | None) -> dict[str, Any] | None:
    """Load explicit ``--manifest`` files, keyed by the content hash they are named for.

    Each path must be ``<sha256-hex>.c2pa.json`` — the same naming rule as a sidecar, so
    an explicitly-passed manifest cannot be bound to media it was not named for.
    """
    if not paths:
        return None
    from novafabric.trust.provenance.c2pa_bind import (
        SIDECAR_SUFFIX,
        normalise_content_hash,
    )

    loaded: dict[str, Any] = {}
    for path in paths:
        if not path.exists():
            err_console.print(f"[red]Manifest not found:[/red] {path}")
            raise typer.Exit(2)
        if not path.name.endswith(SIDECAR_SUFFIX):
            err_console.print(
                f"[red]Manifest must be named <sha256-hex>{SIDECAR_SUFFIX}:[/red] {path.name}"
            )
            raise typer.Exit(2)
        content_hash = normalise_content_hash(path.name[: -len(SIDECAR_SUFFIX)])
        if content_hash is None:
            err_console.print(
                f"[red]Manifest filename is not a sha256 hex digest:[/red] {path.name}"
            )
            raise typer.Exit(2)
        try:
            loaded[content_hash] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            err_console.print(f"[red]Could not read manifest {path}:[/red] {exc}")
            raise typer.Exit(2) from exc
    return loaded


@app.command("bind")
def bind(
    capsule: Annotated[
        Path, typer.Option("--capsule", help="Capsule directory to bind manifests in.")
    ],
    manifest: Annotated[
        list[Path] | None,
        typer.Option(
            "--manifest",
            help=(
                "Explicit manifest file named <sha256-hex>.c2pa.json. Repeatable. "
                "Overrides sidecar discovery under outputs/."
            ),
        ),
    ] = None,
    output_hash: Annotated[
        list[str] | None,
        typer.Option(
            "--output-hash",
            help="content_hash of media the agent PRODUCED (NF-163). Repeatable.",
        ),
    ] = None,
    producing_model: Annotated[
        str | None, typer.Option("--producing-model", help="NF-163 producing model id.")
    ] = None,
    producing_run_id: Annotated[
        str | None, typer.Option("--producing-run-id", help="NF-163 producing run id.")
    ] = None,
    art50_marking_claimed: Annotated[
        bool,
        typer.Option(
            "--art50-marking-claimed/--no-art50-marking-claimed",
            help="Record the producer's Art. 50 marking claim on output entries.",
        ),
    ] = False,
    nf094_receipt_digest: Annotated[
        str | None,
        typer.Option(
            "--nf094-receipt-digest",
            help="Digest of the run-level NF-094 Art. 50 receipt to cross-link.",
        ),
    ] = None,
    capture_manifests: Annotated[
        bool,
        typer.Option(
            "--capture-manifests",
            help="Record that manifest bytes were retained (ADR-0125 opt-in).",
        ),
    ] = False,
    write: Annotated[
        bool,
        typer.Option("--write", help=f"Persist the facet into {_MANIFEST_NAME}."),
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the facet as JSON.")
    ] = False,
) -> None:
    """Bind discoverable C2PA manifests to captured media hashes (NF-161/163).

    Fail-open: a capsule with no manifests produces no facet and exits 0 — absent
    material is not an error (ADR-0148 I-3).

    \b
    Examples:
      nova provenance bind --capsule runs/run_1
      nova provenance bind --capsule runs/run_1 --manifest aa..bb.c2pa.json --write
      nova provenance bind --capsule runs/run_1 --output-hash sha256:aa.. \\
          --producing-model img-gen-v3 --producing-run-id run_1 --art50-marking-claimed
    """
    from novafabric.trust.provenance.c2pa_bind import attach_facet, build_facet

    capsule_dir = _capsule_dir(capsule)
    documents = _load_manifest_documents(manifest)

    facet = build_facet(
        capsule_dir,
        manifests=documents,
        output_hashes=output_hash or (),
        producing_model=producing_model,
        producing_run_id=producing_run_id,
        art50_marking_claimed=art50_marking_claimed if output_hash else None,
        nf094_receipt_digest=nf094_receipt_digest,
        capture_manifest_bytes=capture_manifests,
    )

    if facet is None:
        if json_out:
            print(json.dumps({"media_provenance": None, "bound": 0}, indent=2))
        else:
            console.print(
                "No provenance manifest bound. Nothing was found to bind — this is not "
                "a finding that the media carries no provenance."
            )
            console.print(f"[dim]{HONESTY_LINE}[/dim]")
        raise typer.Exit(0)

    if write:
        capsule_manifest = _read_manifest(capsule_dir)
        attach_facet(capsule_manifest, facet)
        _write_manifest(capsule_dir, capsule_manifest)

    body = facet.model_dump(mode="json", exclude_none=True)
    if json_out:
        print(json.dumps(body, indent=2))
        raise typer.Exit(0)

    console.print(
        f"Bound [bold]{len(facet.entries)}[/bold] manifest(s) across "
        f"{facet.media_parts_scanned} media part(s); "
        f"{facet.manifests_found} manifest(s) discovered."
    )
    for entry in facet.entries:
        console.print(
            f"  {entry.direction:6} {entry.bound_content_hash[:19]}… "
            f"kind={entry.manifest_kind} bound_against={entry.bound_against} "
            f"hard_binding_ok={entry.verified.hard_binding_ok}"
        )
    if write:
        console.print(f"[green]Wrote[/green] facets.media_provenance to {_MANIFEST_NAME}")
    else:
        console.print("[dim]Not written. Pass --write to persist.[/dim]")
    console.print(f"[dim]{HONESTY_LINE}[/dim]")


@app.command("show")
def show(
    capsule: Annotated[Path, typer.Option("--capsule", help="Capsule directory.")],
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the facet as JSON.")
    ] = False,
) -> None:
    """Print the ``media_provenance`` facet a capsule already carries (NF-161/163).

    \b
    Examples:
      nova provenance show --capsule runs/run_1
      nova provenance show --capsule runs/run_1 --json
    """
    from novafabric.trust.provenance.c2pa_bind import facet_from_capsule

    capsule_dir = _capsule_dir(capsule)
    facet = facet_from_capsule(_read_manifest(capsule_dir))

    if facet is None:
        if json_out:
            print(json.dumps({"media_provenance": None}, indent=2))
        else:
            console.print("No media_provenance facet in this capsule.")
            console.print(f"[dim]{HONESTY_LINE}[/dim]")
        raise typer.Exit(0)

    if json_out:
        print(json.dumps(facet.model_dump(mode="json", exclude_none=True), indent=2))
        raise typer.Exit(0)

    console.print(
        f"media_provenance v{facet.schema_version} — {len(facet.entries)} entry(ies), "
        f"{facet.media_parts_scanned} media part(s) scanned"
    )
    for entry in facet.entries:
        signer = entry.signer.subject if entry.signer else "—"
        console.print(
            f"  {entry.direction:6} {entry.bound_content_hash[:19]}… "
            f"kind={entry.manifest_kind} signer={signer} "
            f"manifest={entry.manifest_digest[:19]}…"
        )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")


@app.command("verify")
def verify(
    capsule: Annotated[Path, typer.Option("--capsule", help="Capsule directory.")],
    strict: Annotated[
        bool,
        typer.Option(
            "--strict",
            help="Exit 1 when any binding is not established. Off by default: this "
            "command reports, it does not gate.",
        ),
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the verdicts as JSON.")
    ] = False,
) -> None:
    """Report ``active_manifest_ok`` / ``hard_binding_ok`` / ``cert_chain_ok`` per entry.

    ``cert_chain_ok`` is reported as ``unknown`` for every entry: no offline X.509 chain
    verifier ships with NovaFabric, and it is never inferred from a signature being
    present.

    \b
    Examples:
      nova provenance verify --capsule runs/run_1
      nova provenance verify --capsule runs/run_1 --strict
    """
    from novafabric.trust.provenance.c2pa_bind import (
        facet_from_capsule,
        unverified_bindings,
    )

    capsule_dir = _capsule_dir(capsule)
    facet = facet_from_capsule(_read_manifest(capsule_dir))

    if facet is None:
        if json_out:
            print(json.dumps({"media_provenance": None, "verified": []}, indent=2))
        else:
            console.print("No media_provenance facet to verify.")
            console.print(f"[dim]{HONESTY_LINE}[/dim]")
        raise typer.Exit(0)

    unestablished = unverified_bindings(facet)
    if json_out:
        print(
            json.dumps(
                {
                    "entries": len(facet.entries),
                    "unestablished": len(unestablished),
                    "verified": [
                        {
                            "bound_content_hash": e.bound_content_hash,
                            "direction": e.direction,
                            "bound_against": e.bound_against,
                            **e.verified.model_dump(mode="json"),
                        }
                        for e in facet.entries
                    ],
                    "honesty": HONESTY_LINE,
                },
                indent=2,
            )
        )
        raise typer.Exit(1 if (strict and unestablished) else 0)

    for entry in facet.entries:
        hard = entry.verified.hard_binding_ok
        label = (
            "[green]ok[/green]"
            if hard is True
            else ("[red]FAILED[/red]" if hard is False else "[yellow]not claimed[/yellow]")
        )
        console.print(
            f"  {entry.direction:6} {entry.bound_content_hash[:19]}… "
            f"active_manifest_ok={entry.verified.active_manifest_ok} "
            f"hard_binding={label} (against {entry.bound_against}) "
            f"cert_chain=unknown ({entry.verified.cert_chain_reason})"
        )
    console.print(
        f"{len(facet.entries)} entry(ies), {len(unestablished)} with no established binding."
    )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")
    raise typer.Exit(1 if (strict and unestablished) else 0)


watermark_app = typer.Typer(
    name="watermark",
    help="Watermark-presence CLAIMS (NF-162, pattern-only — no detector ships here).",
    no_args_is_help=True,
)
app.add_typer(watermark_app, name="watermark")


@watermark_app.command("show")
def watermark_show(
    capsule: Annotated[Path, typer.Option("--capsule", help="Capsule directory.")],
    bind_now: Annotated[
        bool,
        typer.Option(
            "--bind",
            help="Read claims from discoverable manifests instead of the stored facet.",
        ),
    ] = False,
    write: Annotated[
        bool,
        typer.Option("--write", help=f"With --bind, persist into {_MANIFEST_NAME}."),
    ] = False,
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the facet as JSON.")
    ] = False,
) -> None:
    """Print recorded watermark-presence claims (NF-162).

    ``present`` has three values and ``unknown`` is not ``false``: it means no claim
    exists, not that a detector found nothing.

    \b
    Examples:
      nova provenance watermark show --capsule runs/run_1
      nova provenance watermark show --capsule runs/run_1 --bind --write
    """
    from novafabric.trust.provenance.watermark import (
        attach_facet,
        build_facet,
        facet_from_capsule,
    )

    capsule_dir = _capsule_dir(capsule)
    if bind_now:
        facet = build_facet(capsule_dir)
        if facet is not None and write:
            capsule_manifest = _read_manifest(capsule_dir)
            attach_facet(capsule_manifest, facet)
            _write_manifest(capsule_dir, capsule_manifest)
    else:
        facet = facet_from_capsule(_read_manifest(capsule_dir))

    if facet is None:
        if json_out:
            print(json.dumps({"watermark_presence": None}, indent=2))
        else:
            console.print(
                "No watermark-presence claim recorded. No claim is not a claim of absence."
            )
            console.print(f"[dim]{HONESTY_LINE}[/dim]")
        raise typer.Exit(0)

    if json_out:
        print(json.dumps(facet.model_dump(mode="json"), indent=2))
        raise typer.Exit(0)

    console.print(
        f"watermark_presence v{facet.schema_version} — {len(facet.entries)} claim(s), "
        f"{facet.media_parts_scanned} media part(s) scanned"
    )
    for entry in facet.entries:
        present = "unknown" if entry.present is None else str(entry.present).lower()
        console.print(
            f"  {entry.bound_content_hash[:19]}… method={entry.method} "
            f"present={present} source={entry.source_of_claim}"
        )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")


@app.command("output")
def output(
    capsule: Annotated[Path, typer.Option("--capsule", help="Capsule directory.")],
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit the receipts as JSON.")
    ] = False,
) -> None:
    """Print the per-artifact output-media receipts and their NF-094 cross-link (NF-163).

    \b
    Examples:
      nova provenance output --capsule runs/run_1
      nova provenance output --capsule runs/run_1 --json
    """
    from novafabric.trust.provenance.c2pa_bind import facet_from_capsule, output_entries

    capsule_dir = _capsule_dir(capsule)
    facet = facet_from_capsule(_read_manifest(capsule_dir))
    entries = output_entries(facet) if facet is not None else []

    if json_out:
        print(
            json.dumps(
                {
                    "output_media_provenance": [
                        e.model_dump(mode="json", exclude_none=True) for e in entries
                    ],
                    "honesty": HONESTY_LINE,
                },
                indent=2,
            )
        )
        raise typer.Exit(0)

    if not entries:
        console.print("No output-media provenance receipts in this capsule.")
        console.print(f"[dim]{HONESTY_LINE}[/dim]")
        raise typer.Exit(0)

    for entry in entries:
        claimed = (
            "unknown"
            if entry.art50_marking_claimed is None
            else str(entry.art50_marking_claimed).lower()
        )
        console.print(
            f"  {entry.bound_content_hash[:19]}… "
            f"model={entry.producing_model or '—'} run={entry.producing_run_id or '—'} "
            f"art50_marking_claimed={claimed} "
            f"nf094_receipt={(entry.nf094_receipt_digest or '—')[:19]}…"
        )
    console.print(f"[dim]{HONESTY_LINE}[/dim]")
