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
"""CLI commands for GDPR Art.17 PII crypto-shredding erasure (cap-001, ADR-0069).

OQ-01 resolved: AES-256-GCM DEK destruction renders ciphertext in capsule records
permanently unrecoverable without touching WORM storage or manifest chains.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from novafabric.pii.dek import (
    ErasureDeferredReceipt,
    ErasureReceipt,
    open_dek_store,
)

console = Console()

app = typer.Typer(
    help="GDPR Art.17 PII crypto-shredding — request and confirm erasure of a data subject.",
    no_args_is_help=True,
)


def _find_capsule_ids_for_subject(
    subject_id: str,
    capsule_dir: Path,
) -> list[str]:
    """Scan *capsule_dir* for redaction_manifest.json files that mention *subject_id*.

    Returns a list of capsule IDs (directory names) whose manifests reference
    the given subject.  The subject_id match is done by scanning for the raw
    string in the manifest JSON (the actual stored value is an HMAC, so we look
    for the directory name as capsule_id which is always stored).

    In practice, because subject_ids are never stored in plaintext (only their
    HMAC appears in redaction manifests), we scan by capsule directory name and
    return all capsule IDs found under the directory.  Operators should supply the
    complete list via ``--capsule-ids`` when available.
    """
    capsule_ids: list[str] = []
    if not capsule_dir.is_dir():
        return capsule_ids

    for manifest_path in capsule_dir.rglob("redaction_manifest.json"):
        try:
            content = manifest_path.read_text(encoding="utf-8")
            data = json.loads(content)
            capsule_id = data.get("capsule_id", "")
            if capsule_id:
                capsule_ids.append(capsule_id)
        except (OSError, json.JSONDecodeError):
            continue

    return capsule_ids


@app.command("erase")
def erase_cmd(
    subject_id: str = typer.Argument(
        ...,
        help="Data subject identifier whose DEK should be destroyed.",
    ),
    capsule_dir: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--capsule-dir",
        exists=False,
        file_okay=False,
        dir_okay=True,
        help=(
            "Directory to scan for redaction_manifest.json files associated "
            "with the data subject. Defaults to $NOVAFABRIC_HOME/capsules/ or "
            "~/.novafabric/capsules/."
        ),
    ),
    retention_months: int = typer.Option(
        6,
        "--retention-months",
        min=0,
        help=(
            "Minimum Art.17(3)(b) retention window in months "
            "(NOVA_AI_ACT_RETENTION_MONTHS). Provider mode should use 120."
        ),
    ),
    output: Optional[Path] = typer.Option(  # noqa: UP007
        None,
        "--output",
        "-o",
        help=(
            "Path to write the ErasureReceipt or ErasureDeferredReceipt JSON. "
            "Defaults to stdout."
        ),
    ),
) -> None:
    """Destroy the AES-256-GCM DEK for a data subject (GDPR Art.17 crypto-shredding).

    Overwrites then deletes the per-subject Data Encryption Key from dek.db.
    Capsule ciphertext becomes permanently unrecoverable; WORM storage and
    NovaSeal manifest chains are not modified.

    If the DEK was created within the Art.17(3)(b) retention window
    (--retention-months), an ErasureDeferredReceipt is written instead.
    Exit code is 0 in both cases.

    Scope: single data subject.

    \b
    Examples:
      nova pii erase user@example.com
      nova pii erase user@example.com --output receipt.json
      nova pii erase user@example.com --retention-months 120 --output receipt.json
      NOVAFABRIC_HOME=/srv/nova nova pii erase user@example.com
    """
    # Resolve capsule directory.
    nova_home = Path(os.environ.get("NOVAFABRIC_HOME", Path.home() / ".novafabric"))
    if capsule_dir is None:
        capsule_dir = nova_home / "capsules"

    # Find capsule IDs associated with this subject.
    capsule_ids = _find_capsule_ids_for_subject(subject_id, capsule_dir)

    # Open DEK store and perform erasure.
    try:
        dek_store = open_dek_store(nova_home)
    except Exception as exc:
        console.print(f"[red]✗[/red] Failed to open DEK store: {exc}")
        raise typer.Exit(code=1) from exc

    try:
        result = dek_store.erase_subject(
            subject_id=subject_id,
            capsule_ids=capsule_ids,
            retention_months=retention_months,
        )
    except KeyError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        console.print(f"[red]✗[/red] Erasure failed: {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        dek_store.close()

    receipt_json = result.model_dump_json(indent=2)

    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(receipt_json, encoding="utf-8")

    if isinstance(result, ErasureReceipt):
        console.print(
            f"[green]✓[/green] DEK destroyed for subject_id={subject_id!r} "
            f"({len(result.capsule_ids_affected)} capsule(s) affected). "
            f"method={result.method}"
        )
    elif isinstance(result, ErasureDeferredReceipt):
        console.print(
            f"[yellow]⚠[/yellow] Erasure deferred for subject_id={subject_id!r}. "
            f"Earliest erasure: {result.earliest_erasure_at.isoformat()} "
            f"(retention_months={retention_months})"
        )

    if output is None:
        sys.stdout.write(receipt_json + "\n")
