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
"""Auto-generated, sealed system/audit card (ADR-0085, gap-014).

A *system card* (src-807) / *audit card* (src-808) is a single document that
summarizes an AI run by assembling facts from the run capsule, its eval result,
and its lineage subgraph. It is **generated, never hand-written** — every field
is derived from on-disk facts at generation time, so the card cannot drift from
the capsule it describes.

The card is **sealed** by reusing the existing DSSE / Ed25519 signing path
(``novafabric.evidence.intoto`` + ``novafabric.evidence.signing``). No new crypto
is implemented here: the card is the DSSE subject, wrapped in an in-toto Statement,
and verifies with the same ``dsse_verify`` + ``verify_with_pem`` path used for
Evidence Bundles (ADR-0011).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from importlib.metadata import version as _pkg_version
from pathlib import Path
from typing import Any

from novafabric.evidence.intoto import dsse_sign, make_intoto_statement

SYSTEM_CARD_PREDICATE_TYPE = "https://novafabric.io/system-card/v0"


def _nova_version() -> str:
    try:
        return _pkg_version("novafabric")
    except Exception:  # pragma: no cover - packaging edge case
        return "0.0.0"


class SystemCardGenerator:
    """Assemble and seal a system/audit card from capsule + eval + lineage facts."""

    def build_card(self, capsule_dir: Path) -> dict[str, Any]:
        """Build the card dict from on-disk facts under *capsule_dir*.

        Reads ``capsule.yaml`` (required), ``eval_result.json`` (optional), and
        ``lineage.jsonl`` (optional). Missing optional inputs degrade cleanly.
        """
        manifest = self._load_manifest(capsule_dir)

        card: dict[str, Any] = {
            "schema_version": "0.1.0",
            "card_type": "system-audit",
            "run_id": manifest.get("run_id", "unknown"),
            "model": {
                "name": manifest.get("model") or manifest.get("model_id", "unknown"),
                "provider": manifest.get("provider", ""),
                "version": manifest.get("model_version", ""),
            },
            "evaluation": self._eval_facts(capsule_dir),
            "lineage": {"edge_count": self._lineage_edge_count(capsule_dir)},
            "generated_by": {"tool": "novafabric", "version": _nova_version()},
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
        return card

    def seal_card(self, card: dict[str, Any], signer: Any) -> dict[str, Any]:
        """Wrap *card* in a signed DSSE envelope using the existing signer.

        ``signer`` must satisfy the ``dsse_sign`` protocol (``sign(bytes) -> bytes``
        and ``keyid: str``), e.g. ``novafabric.evidence.signing.LocalSigner``.

        The card is canonicalized, sha256'd, and used as the in-toto Statement
        subject; the card itself is the statement predicate. No new crypto.
        """
        canonical = json.dumps(card, sort_keys=True, separators=(",", ":")).encode()
        card_digest = "sha256:" + hashlib.sha256(canonical).hexdigest()
        statement = make_intoto_statement(
            predicate_type=SYSTEM_CARD_PREDICATE_TYPE,
            subject_name=f"system-card/{card.get('run_id', 'unknown')}",
            subject_sha256=card_digest,
            predicate=card,
        )
        return dsse_sign(statement, signer)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _load_manifest(self, capsule_dir: Path) -> dict[str, Any]:
        manifest_path = capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return {}
        import yaml

        with open(manifest_path) as fh:
            return yaml.safe_load(fh) or {}

    def _eval_facts(self, capsule_dir: Path) -> dict[str, Any] | None:
        eval_path = capsule_dir / "eval_result.json"
        if not eval_path.exists():
            return None
        try:
            data = json.loads(eval_path.read_text())
        except Exception:
            return None
        return {
            "suite_id": data.get("suite_id"),
            "passed": data.get("passed"),
            "metrics": data.get("metrics", []),
            # Carry version pins forward if the eval result recorded them (ADR-0085).
            "asset_version": data.get("asset_version"),
            "dataset_version": data.get("dataset_version"),
        }

    def _lineage_edge_count(self, capsule_dir: Path) -> int:
        lineage_path = capsule_dir / "lineage.jsonl"
        if not lineage_path.exists():
            return 0
        return sum(1 for line in lineage_path.read_text().splitlines() if line.strip())
