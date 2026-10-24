"""A re-performance attestation as ``nova evidence attest-replay`` writes it: DSSE-signed.

The compiler counts one only when it verifies under a trusted key (review misc-1-02),
so fixtures sign theirs and hand the public key to ``SafetyCaseCompiler``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from novafabric.evidence.intoto import dsse_sign, make_intoto_statement
from novafabric.evidence.reperformance import REPERFORMANCE_PREDICATE_TYPE
from novafabric.evidence.signing import LocalSigner, generate_keypair


def write_replay(
    cap: Path, key_dir: Path, *, match: str = "exact", mode: str = "exact", **predicate: Any
) -> bytes:
    """Sign a replay attestation of *cap*'s current ``capsule.yaml``; return the public PEM."""
    priv, pub = generate_keypair(key_dir)
    statement = make_intoto_statement(
        predicate_type=REPERFORMANCE_PREDICATE_TYPE,
        subject_name=f"run-capsule/{cap.name}",
        subject_sha256=hashlib.sha256((cap / "capsule.yaml").read_bytes()).hexdigest(),
        predicate={"run_id": cap.name, "match": match, "replay_mode": mode, **predicate},
    )
    path = cap / "attestations" / "reperformance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dsse_sign(statement, LocalSigner(priv)), indent=2))
    return pub.read_bytes()
