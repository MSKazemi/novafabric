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

"""``nova migrate-format`` — record a format-migration hop (ADR-0165 P2, NF-332).

Record-only. The command appends a hop to the ``format_migration_chain`` of a
preservation facet and walks the chain offline back to ``original_root``. It
does **not** run a migrator, and it **never writes to a stored capsule**:
rewriting ``capsule.yaml`` in place would change the bytes the capsule's seal
covers. The updated facet is emitted as JSON (stdout, or a new ``--output``
file); binding it into a sealed Evidence Bundle is later ADR-0165 work.

Streams: the facet JSON (or the ``--check --json`` report) goes to stdout so it
can be piped; human messages and the in-mission-boundary line go to stderr.

Exit codes: 0 recorded / chain ok; 1 chain broken or hop refused; 2 bad input.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.preservation.anchor import PreservationError, PreservationFacet
from novafabric.preservation.format_migration import (
    BrokenMigrationChainError,
    FormatMigrationVerification,
    append_format_migration,
    chain_from_facet,
    plan_next_hop,
    verify_format_migration_chain,
)

# soft_wrap: never hard-wrap paths or digests mid-token in error output.
err_console = Console(stderr=True, soft_wrap=True)

#: Spec §3 req. 5: every ``migrate-format`` output carries this line.
BOUNDARY_LINE = (
    "NovaFabric records and re-verifies format-migration provenance only; it did "
    "not run the migrator, did not modify any stored capsule, and makes no claim "
    "that the migration was faithful, authorized, or regulator-accepted "
    "(ADR-0165 I-4)."
)

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2

_READ_CHUNK = 1 << 20


class _InputError(Exception):
    """A user-input problem, rendered and mapped to exit code 2."""


def _describe(exc: Exception) -> str:
    """Render an error without echoing the offending input.

    Pydantic's default rendering repeats the rejected value, which is exactly
    wrong when the value was rejected for embedding a credential (I-2).
    """
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        )
    return str(exc)


def _digest_file(path: Path) -> str:
    """Stream-hash a file to ``sha256:<hex>``; the bytes are not retained (I-2)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _load_from_capsule(ref: str) -> tuple[PreservationFacet, str | None]:
    """Read ``facets.preservation`` from a capsule, read-only.

    Returns the facet and the capsule's ``run-capsule@<schema_version>``, the
    natural ``from_version`` of a first hop.
    """
    try:
        capsule_dir = resolve_capsule_ref(ref)
    except CapsuleRefError as exc:
        raise _InputError(str(exc)) from exc
    try:
        manifest = yaml.safe_load((capsule_dir / "capsule.yaml").read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise _InputError(f"cannot read capsule.yaml in {capsule_dir}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise _InputError(f"capsule.yaml in {capsule_dir} is not a mapping")
    facets = manifest.get("facets")
    block = facets.get("preservation") if isinstance(facets, dict) else None
    if not isinstance(block, dict):
        raise _InputError(
            f"capsule {capsule_dir.name} has no facets.preservation anchor "
            "(NF-331); a format-migration chain appends to an anchor. Build one "
            "with novafabric.preservation.build_anchor, or pass --facet."
        )
    version = manifest.get("schema_version")
    from_version = f"run-capsule@{version}" if isinstance(version, str) else None
    return _validate_facet(block), from_version


def _load_from_file(path: Path) -> PreservationFacet:
    """Read a standalone preservation facet JSON document."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise _InputError(f"cannot read facet file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise _InputError(f"facet file {path} must hold a JSON object")
    return _validate_facet(data)


def _validate_facet(data: dict[str, Any]) -> PreservationFacet:
    try:
        return PreservationFacet.model_validate(data)
    except (ValueError, PreservationError) as exc:
        raise _InputError(f"preservation facet is invalid: {_describe(exc)}") from exc


def _render_findings(verification: FormatMigrationVerification) -> None:
    for finding in verification.findings:
        err_console.print(
            f"  [red]hop {finding.hop_index}[/red] {finding.code}: {finding.message}",
            highlight=False,
        )


def _summary(verification: FormatMigrationVerification) -> str:
    return (
        f"hops={verification.hop_count} chain_walk_ok={verification.chain_walk_ok} "
        f"reaches_original_root={verification.reaches_original_root} "
        f"acyclic={verification.acyclic} monotonic={verification.monotonic}"
    )


def _check(facet: PreservationFacet, json_out: bool) -> int:
    verification = verify_format_migration_chain(chain_from_facet(facet), facet.original_root)
    if json_out:
        payload = verification.model_dump(mode="json")
        payload["ok"] = verification.ok
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    status = "[green]OK[/green]" if verification.ok else "[red]BROKEN[/red]"
    err_console.print(f"Format-migration chain {status}: {_summary(verification)}")
    _render_findings(verification)
    return EXIT_OK if verification.ok else EXIT_BROKEN


def _write_output(facet: PreservationFacet, output: Path | None) -> None:
    text = json.dumps(facet.model_dump(mode="json", exclude_none=True), indent=2) + "\n"
    if output is None:
        typer.echo(text, nl=False)
        return
    try:
        # "x" — create only. An existing file may be an earlier chain version,
        # and silently overwriting it would destroy the record being extended.
        with output.open("x", encoding="utf-8") as handle:
            handle.write(text)
    except FileExistsError as exc:
        raise _InputError(f"--output {output} already exists; refusing to overwrite") from exc
    except OSError as exc:
        raise _InputError(f"cannot write --output {output}: {exc}") from exc
    err_console.print(f"Wrote updated preservation facet to {output}", highlight=False)


def _resolve_post_digest(post_digest: str | None, migrated_artifact: Path | None) -> str:
    if (post_digest is None) == (migrated_artifact is None):
        raise _InputError("pass exactly one of --post-digest or --migrated-artifact")
    if migrated_artifact is not None:
        if not migrated_artifact.is_file():
            raise _InputError(f"--migrated-artifact {migrated_artifact} is not a file")
        return _digest_file(migrated_artifact)
    assert post_digest is not None  # narrowed by the exactly-one check above
    return post_digest


def _run(
    *,
    capsule: str | None,
    facet_path: Path | None,
    to_version: str | None,
    tool: str | None,
    post_digest: str | None,
    migrated_artifact: Path | None,
    from_version: str | None,
    migrated_at: str | None,
    output: Path | None,
    check: bool,
    json_out: bool,
) -> int:
    if (capsule is None) == (facet_path is None):
        raise _InputError("pass exactly one of --capsule or --facet")
    capsule_from: str | None = None
    if capsule is not None:
        facet, capsule_from = _load_from_capsule(capsule)
    else:
        assert facet_path is not None  # narrowed by the exactly-one check above
        facet = _load_from_file(facet_path)

    try:
        if check:
            return _check(facet, json_out)
        if to_version is None or tool is None:
            raise _InputError("--to and --tool are required unless --check is given")
        existing = chain_from_facet(facet)
        hop = plan_next_hop(
            facet,
            to_version=to_version,
            tool_ref=tool,
            post_digest=_resolve_post_digest(post_digest, migrated_artifact),
            migrated_at=migrated_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            from_version=from_version or (None if existing else capsule_from),
        )
        updated = append_format_migration(facet, hop)
    except BrokenMigrationChainError as exc:
        err_console.print(f"[red]Refused:[/red] {exc}", highlight=False)
        _render_findings(exc.verification)
        return EXIT_BROKEN
    except (ValueError, PreservationError) as exc:
        raise _InputError(_describe(exc)) from exc

    _write_output(updated, output)
    err_console.print(
        f"Recorded hop {len(existing)}: {hop.from_version} → {hop.to_version} "
        f"(post_digest {hop.post_digest})",
        highlight=False,
    )
    return EXIT_OK


def migrate_format_cmd(
    capsule: Annotated[
        str | None,
        typer.Option(
            "--capsule",
            help="Capsule directory or run id whose facets.preservation anchor to "
            "read (read-only; the capsule is never modified).",
            show_default=False,
        ),
    ] = None,
    facet: Annotated[
        Path | None,
        typer.Option(
            "--facet",
            help="A standalone preservation-facet JSON document to read instead of a capsule.",
            show_default=False,
        ),
    ] = None,
    to_version: Annotated[
        str | None,
        typer.Option(
            "--to",
            help="Format the artifact was migrated to, e.g. run-capsule@0.3.0.",
            show_default=False,
        ),
    ] = None,
    tool: Annotated[
        str | None,
        typer.Option(
            "--tool",
            help="Reference to the migrator: sha256:<hex> digest or URI "
            "(never the migrator itself).",
            show_default=False,
        ),
    ] = None,
    post_digest: Annotated[
        str | None,
        typer.Option(
            "--post-digest",
            help="sha256:<hex> digest of the migrated artifact.",
            show_default=False,
        ),
    ] = None,
    migrated_artifact: Annotated[
        Path | None,
        typer.Option(
            "--migrated-artifact",
            help="Path to the migrated artifact; it is hashed (not copied) to "
            "obtain the post-digest.",
            show_default=False,
        ),
    ] = None,
    from_version: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Format migrated from. Defaults to the previous hop's --to, or, "
            "for the first hop of a --capsule, run-capsule@<schema_version>.",
            show_default=False,
        ),
    ] = None,
    migrated_at: Annotated[
        str | None,
        typer.Option(
            "--migrated-at",
            help="RFC 3339 time of the migration (default: now, UTC).",
            show_default=False,
        ),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Write the updated facet JSON to this new file (must not exist). Default: stdout.",
            show_default=False,
        ),
    ] = None,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Only walk the existing chain offline and report; append nothing.",
        ),
    ] = False,
    json_out: Annotated[
        bool,
        typer.Option("--json", help="With --check, print the verification as JSON."),
    ] = False,
) -> None:
    """Record a format-migration hop in a preservation facet (NF-332, experimental).

    Appends {from_version, to_version, migrated_at, tool_ref, pre_digest,
    post_digest, parent} to facets.preservation.format_migration_chain after
    walking the chain offline back to original_root. parent and pre_digest are
    derived, never supplied. Refuses (exit 1) to extend a broken chain or to
    append a hop that is non-monotonic or cyclic.

    Record-only: it does not run the migrator and never modifies a stored
    capsule; the updated facet is emitted as JSON.

    \b
    Examples:
      nova migrate-format --capsule 01HX... --to run-capsule@0.3.0 \\
          --tool sha256:<migrator> --migrated-artifact migrated/capsule.yaml \\
          -o preservation.json
      nova migrate-format --facet preservation.json --check --json
    """
    try:
        code = _run(
            capsule=capsule,
            facet_path=facet,
            to_version=to_version,
            tool=tool,
            post_digest=post_digest,
            migrated_artifact=migrated_artifact,
            from_version=from_version,
            migrated_at=migrated_at,
            output=output,
            check=check,
            json_out=json_out,
        )
    except _InputError as exc:
        err_console.print(f"[red]Error:[/red] {exc}", highlight=False)
        code = EXIT_USAGE
    err_console.print(f"[dim]{BOUNDARY_LINE}[/dim]", highlight=False)
    raise typer.Exit(code)
