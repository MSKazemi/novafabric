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

"""``nova verify-envelope`` — verify a DSSE outer envelope (NF-029/030/031, ADR-0096).

A one-stop helper that verifies any NovaFabric DSSE envelope (Evidence Bundle wrap,
in-toto capsule Statement, or SLSA provenance) with a local Ed25519 public key — the
same verdict a third party gets from stock `cosign verify-blob-attestation`. Reads the
envelope JSON, recomputes the DSSE PAE, and checks the Ed25519 signature via the single
DSSE verifier in :mod:`novafabric.evidence.intoto`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from rich.console import Console

from novafabric.evidence.intoto import dsse_verify

console = Console()


def _load_public_key(key_path: Path) -> Ed25519PublicKey:
    """Load an Ed25519 public key from a PEM file (accepts a private-key PEM too)."""
    data = key_path.read_bytes()
    try:
        loaded: object = serialization.load_pem_public_key(data)
    except ValueError:
        priv = serialization.load_pem_private_key(data, password=None)
        loaded = priv.public_key()
    if not isinstance(loaded, Ed25519PublicKey):
        raise typer.BadParameter(f"{key_path} is not an Ed25519 key")
    return loaded


def verify_envelope_cmd(
    envelope: Annotated[Path, typer.Argument(help="Path to a DSSE envelope JSON file.")],
    key: Annotated[
        Path, typer.Option("--key", help="PEM-encoded Ed25519 public (or private) key.")
    ],
) -> None:
    """Verify a DSSE envelope's Ed25519 signature (exit non-zero on failure)."""
    env = json.loads(envelope.read_text(encoding="utf-8"))
    public_key = _load_public_key(key)

    def _verify(pae: bytes, sig: bytes) -> bool:
        try:
            public_key.verify(sig, pae)
            return True
        except Exception:
            return False

    try:
        dsse_verify(env, _verify)
    except (ValueError, KeyError, IndexError) as exc:
        console.print(f"[red]✗ envelope verification FAILED[/red]: {exc}")
        raise typer.Exit(code=1) from exc

    keyid = env.get("signatures", [{}])[0].get("keyid", "?")
    console.print(
        f"[green]✓ verified[/green]  payloadType={env.get('payloadType')}  keyid={keyid}"
    )
