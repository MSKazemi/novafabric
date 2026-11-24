"""``ingest_capsule_dir`` carries the capsule's seal state and never lets a broken seal verify.

A caller that knows the tri-state (``absent`` | ``valid`` | ``invalid``) passes it as
``seal_status``; the result echoes it and exposes ``tampered``. Whatever ``novaseal_valid``
says, edges from an ``invalid`` seal are never counted verified.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("kuzu")

from novafabric.kg.capsule_ingest import ingest_capsule_dir  # noqa: E402
from novafabric.kg.pipeline import KGIngestionPipeline  # noqa: E402
from novafabric.kg.store import KGStore  # noqa: E402

CALL = {
    "model_call_id": "m1", "parent_span_id": "span-agent",
    "gen_ai.system": "openai", "gen_ai.request.model": "gpt-4o",
}


def _capsule(root: Path, name: str) -> Path:
    cdir = root / name
    cdir.mkdir(parents=True)
    (cdir / "capsule.yaml").write_text(yaml.safe_dump({"run_id": name}))
    (cdir / "model-calls.jsonl").write_text(json.dumps(CALL) + "\n")
    return cdir


def _confidence(store: KGStore) -> float:
    (edge,) = store.query_agent_models("span-agent")
    return float(edge["confidence"])


def test_no_seal_status_is_not_tampered_and_keeps_the_old_contract(tmp_path: Path) -> None:
    store = KGStore(str(tmp_path / "kg.kuzu"))
    store.init_schema()
    result = ingest_capsule_dir(KGIngestionPipeline(store), _capsule(tmp_path, "c1"))
    assert result.seal_status is None
    assert result.tampered is False
    assert _confidence(store) == 0.0


@pytest.mark.parametrize(
    ("status", "valid", "tampered", "confidence"),
    [
        ("valid", True, False, 1.0),
        ("absent", False, False, 0.0),
        ("invalid", False, True, 0.0),
        ("invalid", True, True, 0.0),  # an inconsistent caller: a broken seal never verifies
    ],
)
def test_seal_status_is_echoed_and_an_invalid_seal_never_verifies(
    tmp_path: Path, status: str, valid: bool, tampered: bool, confidence: float
) -> None:
    store = KGStore(str(tmp_path / "kg.kuzu"))
    store.init_schema()
    result = ingest_capsule_dir(
        KGIngestionPipeline(store), _capsule(tmp_path, "c1"),
        novaseal_valid=valid, seal_status=status,  # type: ignore[arg-type]
    )
    assert result.seal_status == status
    assert result.tampered is tampered
    assert _confidence(store) == confidence
