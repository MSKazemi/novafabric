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

"""``nova dataset`` — dataset supply-chain provenance (NF-058, ADR-0105)."""

from __future__ import annotations

import getpass
import json
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console

console = Console()

dataset_app = typer.Typer(
    name="dataset",
    help="Dataset supply-chain provenance (signed provenance cards, NF-058).",
    no_args_is_help=True,
)


@dataset_app.command("provenance-card")
def provenance_card_cmd(
    asset: Annotated[
        str, typer.Argument(help="Dataset asset reference, e.g. dataset:gaia@2026-05.")
    ],
    source: Annotated[
        str,
        typer.Option("--source", help="Source URI, e.g. oci://…/gaia:2026-05 (configurable)."),
    ],
    version: Annotated[str, typer.Option("--version", help="Dataset version label.")],
    dataset_hash: Annotated[
        str, typer.Option("--hash", help="Dataset content sha256 (bare hex or sha256:…).")
    ],
    retrieved_at: Annotated[
        Optional[str], typer.Option("--retrieved-at", help="RFC 3339 retrieval time.")
    ] = None,
    license_id: Annotated[
        Optional[str], typer.Option("--license", help="SPDX license id, e.g. CC-BY-4.0.")
    ] = None,
    tlp: Annotated[
        Optional[str], typer.Option("--tlp", help="TLP marking, e.g. TLP:CLEAR.")
    ] = None,
    from_capsule: Annotated[
        Optional[Path],
        typer.Option(
            "--from-capsule",
            help="Capsule dir to pull transformHistory from (its lineage derivation edges).",
        ),
    ] = None,
    registry_digest: Annotated[
        Optional[str],
        typer.Option("--registry-digest", help="Content-addressed registry digest to bind to."),
    ] = None,
    sign: Annotated[
        bool, typer.Option("--sign", help="Ed25519-sign the card with the keyring key.")
    ] = False,
    identity: Annotated[
        str, typer.Option(help="Signing identity (keyring key) for --sign.")
    ] = "",
    out: Annotated[
        Optional[Path], typer.Option("--out", "-o", help="Write the card JSON to this path.")
    ] = None,
) -> None:
    """Emit (and optionally sign) a dataset provenance card (NF-058).

    Pulls transform history from a capsule's lineage when ``--from-capsule`` is given;
    signs with the NovaSeal/Evidence-Bundle Ed25519 keyring path when ``--sign`` is set —
    an unsigned card is metadata, a signed card is evidence.

    Scope: single dataset asset.

    \b
    Examples:
      nova dataset provenance-card dataset:gaia@2026-05 --source oci://reg/gaia:2026-05 \\
          --version 2026-05 --hash b17a... --sign
    """
    from novafabric.supplychain.dataset_card import (
        build_card,
        sign_card,
        transforms_from_capsule,
    )

    transforms = transforms_from_capsule(from_capsule) if from_capsule else None
    card = build_card(
        asset=asset,
        source_uri=source,
        version=version,
        dataset_hash=dataset_hash,
        retrieved_at=retrieved_at,
        license=license_id,
        tlp=tlp,
        transforms=transforms,
        registry_digest=registry_digest,
    )

    if sign:
        from novafabric.trust.keyring import ensure_keypair

        signer_id = identity or getpass.getuser()
        private_key, key_fp = ensure_keypair(signer_id)
        card = sign_card(card, private_key, keyid=key_fp)

    payload = json.dumps(card.to_json_dict(), indent=2)
    if out is not None:
        out.write_text(payload + "\n", encoding="utf-8")
        signed_note = " (signed)" if sign else ""
        console.print(f"[green]✓[/green] Dataset provenance card written: {out}{signed_note}")
    else:
        console.print_json(payload)
