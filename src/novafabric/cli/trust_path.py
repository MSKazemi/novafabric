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

"""``nova trust-path show|verify`` — NF-363 transitive trust path (ADR-0168 P2, experimental).

The group name and both verbs are the ones ADR-0168 "CLI / API surface" and
spec §4.4 fix. Both read ``facets.federation.trust_path`` from a capsule.

- ``show`` prints the recorded hops **unverified** — no signature is checked,
  and any ``verified`` flags recorded in the capsule are not echoed as truth.
- ``verify`` walks the path offline against anchors **the verifier pins on the
  command line** (``--anchor ORG=PUBKEY.pem``). The capsule's own
  ``trust_anchor`` pin is never used: a capsule under verification cannot
  choose the root it is verified against.

Exit codes: ``0`` read (show) / path verified (verify); ``1`` the walk failed
— broken, cyclic, reordered or anchor-mismatched hop, unpinned anchor, bad
signature, revoked subject, a malformed path, or no path at all (fail
closed); ``2`` usage or input error (capsule not found/unreadable, bad anchor
file). A depth-constraint overrun is flagged in the output and does not change
the exit code — unless ``--strict-depth`` is given, which makes overrunning a
hop's *signed* ``max_path_length`` fatal (exit 1, ``signed_depth_exceeded``,
OpenID Federation semantics). The verifier's own ``--max-depth`` policy is
always a flag. Every output, JSON included, carries the in-mission-boundary line.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from novafabric.cli._capsule_ref import CapsuleRefError, resolve_capsule_ref
from novafabric.federation.facet import FederationError
from novafabric.federation.trust_path import (
    IN_MISSION_BOUNDARY,
    MAX_ORG_ID_LENGTH,
    PinnedAnchor,
    TrustPath,
    TrustPathError,
    structural_summary,
    trust_path_from_capsule,
    verify_trust_path,
)

__all__ = ["trust_path_app"]

console = Console()
err_console = Console(stderr=True)

_MANIFEST_NAME = "capsule.yaml"
#: Bounded reads: a capsule manifest and a PEM public key are small.
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_ANCHOR_BYTES = 64 * 1024
_MAX_ANCHORS = 32
_MAX_REVOKED = 256

trust_path_app = typer.Typer(
    help=(
        "Transitive cross-org trust path (experimental, ADR-0168 NF-363): show or "
        "offline-verify the signed A->B->C delegation chain recorded in a capsule "
        "against anchors YOU pin. Record/verify only - NovaFabric is never the anchor."
    ),
    no_args_is_help=True,
)

CapsuleOpt = Annotated[
    str,
    typer.Option(
        "--capsule",
        help="Capsule directory, or a bare run id resolved under the capsule dir.",
    ),
]
JsonOpt = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


class _InputError(Exception):
    """A usage/input problem — exit 2."""


def _emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps({"boundary": IN_MISSION_BOUNDARY, **payload}, indent=2))


def _input_error(message: str) -> typer.Exit:
    err_console.print(IN_MISSION_BOUNDARY, markup=False, highlight=False)
    err_console.print(message, markup=False, highlight=False, style="red")
    return typer.Exit(2)


def _read_manifest(capsule: str) -> tuple[Path, dict[str, Any]]:
    """Resolve and read ``capsule.yaml`` with a size bound."""
    try:
        capsule_dir = resolve_capsule_ref(capsule)
    except CapsuleRefError as exc:
        raise _InputError(str(exc)) from exc
    manifest_path = capsule_dir / _MANIFEST_NAME
    try:
        size = manifest_path.stat().st_size
        if size > _MAX_MANIFEST_BYTES:
            raise _InputError(
                f"{_MANIFEST_NAME} is {size} bytes, over the {_MAX_MANIFEST_BYTES}-byte cap"
            )
        manifest = yaml.safe_load(manifest_path.read_text("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise _InputError(f"Could not read {_MANIFEST_NAME}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise _InputError(f"{_MANIFEST_NAME} is not a mapping.")
    return capsule_dir, manifest


def _parse_anchor(spec: str) -> PinnedAnchor:
    """Parse ``ORG=PATH`` into a :class:`PinnedAnchor` from a PEM public key."""
    org, sep, raw_path = spec.partition("=")
    if not sep or not org or not raw_path:
        raise _InputError(f"--anchor must be ORG=PUBKEY.pem, got {spec[:80]!r}")
    path = Path(raw_path)
    try:
        if not path.is_file():
            raise _InputError(f"--anchor file not found: {raw_path}")
        if path.stat().st_size > _MAX_ANCHOR_BYTES:
            raise _InputError(f"--anchor file {raw_path} is over {_MAX_ANCHOR_BYTES} bytes")
        pem = path.read_bytes()
    except OSError as exc:
        raise _InputError(f"Could not read --anchor file {raw_path}: {exc}") from exc
    try:
        return PinnedAnchor.from_pem(org, pem)
    except FederationError as exc:
        raise _InputError(f"--anchor {org[:80]!r}: {exc}") from exc


def _hop_table(path: TrustPath) -> Table:
    table = Table(show_lines=False)
    for col in ("#", "from_org", "to_org", "statement_digest", "anchor_digest", "max_len"):
        table.add_column(col)
    for row in structural_summary(path):
        table.add_row(
            str(row["index"]),
            row["from_org"],
            row["to_org"],
            row["statement_digest"],
            row["anchor_digest"],
            "-" if row["max_path_length"] is None else str(row["max_path_length"]),
        )
    return table


@trust_path_app.command("show")
def show(capsule: CapsuleOpt, as_json: JsonOpt = False) -> None:
    """Print the recorded trust path, UNVERIFIED (no signature is checked).

    Use ``nova trust-path verify`` to walk it against anchors you pin. Exits 0
    whenever the capsule was read, including when it records no path.
    """
    try:
        capsule_dir, manifest = _read_manifest(capsule)
        path = trust_path_from_capsule(manifest)
    except _InputError as exc:
        raise _input_error(str(exc)) from exc
    except FederationError as exc:
        raise _input_error(f"facets.federation.trust_path is malformed: {exc}") from exc
    rows = structural_summary(path) if path else []
    if as_json:
        _emit_json({"capsule": str(capsule_dir), "verified": False, "trust_path": rows})
        return
    console.print(IN_MISSION_BOUNDARY, markup=False, highlight=False)
    if path is None:
        console.print("No trust path recorded (facets.federation.trust_path absent).")
        return
    console.print(
        f"{len(path.hops)} hop(s) in {capsule_dir} - UNVERIFIED; run "
        "`nova trust-path verify --anchor ORG=PUBKEY.pem` to walk it.",
        markup=False,
        highlight=False,
    )
    console.print(_hop_table(path))


@trust_path_app.command("verify")
def verify(
    capsule: CapsuleOpt,
    anchor: Annotated[
        list[str],
        typer.Option(
            "--anchor",
            help="A trust anchor YOU pin, as ORG=PUBKEY.pem (Ed25519 or ECDSA P-256 "
            "public key). Repeatable; required. The capsule's own anchors are never used.",
        ),
    ],
    max_depth: Annotated[
        int | None,
        typer.Option(
            "--max-depth",
            min=0,
            help="Your delegation-depth policy (total hops). Exceeding it is flagged, not fatal.",
        ),
    ] = None,
    revoked: Annotated[
        list[str] | None,
        typer.Option(
            "--revoked",
            help="A revoked subject (org id or sha256 key digest). Repeatable; a path "
            "that transits one fails (path_touches_revoked).",
        ),
    ] = None,
    strict_depth: Annotated[
        bool,
        typer.Option(
            "--strict-depth",
            help="Fail (exit 1) when the path overruns a hop's SIGNED max_path_length "
            "(OpenID Federation semantics). Default: flagged, not fatal (spec 3.7). "
            "Does not make --max-depth fatal.",
        ),
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Walk the recorded trust path offline against anchors you pin (NF-363).

    Passes only when the path is acyclic, has no broken hop, terminates at one of
    your --anchor pins, and touches no --revoked subject (and, with
    --strict-depth, overruns no signed max_path_length). A valid path proves the
    delegation chain composes, not that the foreign evidence is correct.
    """
    revoked_list = revoked or []
    try:
        if len(anchor) > _MAX_ANCHORS:
            raise _InputError(f"at most {_MAX_ANCHORS} --anchor pins are accepted")
        if len(revoked_list) > _MAX_REVOKED:
            raise _InputError(f"at most {_MAX_REVOKED} --revoked subjects are accepted")
        if any(len(r) > MAX_ORG_ID_LENGTH for r in revoked_list):
            raise _InputError(f"--revoked values are capped at {MAX_ORG_ID_LENGTH} chars")
        pins = [_parse_anchor(spec) for spec in anchor]
        capsule_dir, manifest = _read_manifest(capsule)
    except _InputError as exc:
        raise _input_error(str(exc)) from exc

    failure: dict[str, Any] | None = None
    report = None
    try:
        path = trust_path_from_capsule(manifest)
    except FederationError as exc:
        path = None
        failure = {"reason": "malformed_path", "detail": str(exc)}
    if path is None and failure is None:
        failure = {
            "reason": "no_trust_path",
            "detail": "facets.federation.trust_path is absent; nothing to verify",
        }
    if path is not None:
        try:
            report = verify_trust_path(
                path,
                pinned_anchors=pins,
                max_depth=max_depth,
                revoked=revoked_list,
                strict_depth=strict_depth,
            )
        except TrustPathError as exc:  # pragma: no cover - inputs validated above
            failure = {"reason": "verifier_error", "detail": str(exc)}

    ok = report is not None and report.path_walk_ok
    if as_json:
        result: dict[str, Any] = (
            report.model_dump(mode="json")
            if report is not None
            else {"path_walk_ok": False, **(failure or {})}
        )
        _emit_json({"capsule": str(capsule_dir), **result})
    else:
        console.print(IN_MISSION_BOUNDARY, markup=False, highlight=False)
        if report is None:
            assert failure is not None
            console.print(
                f"FAIL {failure['reason']}: {failure['detail']}",
                markup=False,
                highlight=False,
            )
        else:
            _print_report(report)
    if not ok:
        raise typer.Exit(1)


def _print_report(report: Any) -> None:
    verdict = "PASS" if report.path_walk_ok else "FAIL"
    console.print(f"{verdict} trust path ({report.hop_count} hop(s))", markup=False)
    for flag in (
        "acyclic",
        "no_broken_hop",
        "terminates_at_anchor",
        "path_touches_revoked",
        "delegation_depth_exceeded",
    ):
        console.print(f"  {flag}: {str(getattr(report, flag)).lower()}", markup=False)
    if report.reason:
        where = "" if report.broken_hop is None else f" at hop {report.broken_hop}"
        console.print(
            f"  reason: {report.reason}{where} - {report.detail}",
            markup=False,
            highlight=False,
        )
    if report.signed_depth_flags:
        fatal = "fatal, --strict-depth" if report.strict_depth else "flagged, not fatal"
        console.print(
            f"  signed max_path_length exceeded at hop(s): "
            f"{list(report.signed_depth_flags)} ({fatal})",
            markup=False,
        )
    if report.policy_depth_exceeded:
        console.print(
            f"  verifier --max-depth policy exceeded: {report.hop_count} hop(s) "
            "(flagged, not fatal)",
            markup=False,
        )
    if report.revoked_hits:
        console.print(f"  revoked subjects transited: {list(report.revoked_hits)}", markup=False)
    if report.path_walk_ok:
        console.print(
            f"  anchor: {report.anchor_org} ({report.anchor_digest}) -> leaf: "
            f"{report.leaf_org} ({report.leaf_key_digest})",
            markup=False,
            highlight=False,
        )
