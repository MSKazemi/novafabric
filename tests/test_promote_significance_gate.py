"""Tests for the opt-in statistical-significance eval gate wired into
``promote_asset()`` (ADR-0080, gap-004).

Invariant: the significance gate is **opt-in**. With ``significance_gate=False``
(the default) promotion behaves exactly as before — the existing single-score
``_has_passing_eval`` gate stays in force and SPRT is never consulted. With
``significance_gate=True``, an agent promotion to a gated status is blocked only
when a Wald SPRT over the recent pass/fail sequence yields a *statistically
significant* regression (``ACCEPT_H1``); noise (``ACCEPT_H0``) and inconclusive
evidence (``CONTINUE``) do NOT block.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from novafabric.policy._models import PolicyDecision
from novafabric.registry.service import (
    PromotionBlockedError,
    promote_asset,
    register_asset,
)
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _allow_engine() -> MagicMock:
    engine = MagicMock()
    engine.evaluate.return_value = PolicyDecision(
        allow=True, reason="ok", decision_id="dec-allow"
    )
    return engine


def _register_agent(tmp_db: Path, fixtures_dir: Path) -> str:
    """Register the valid agent fixture; return its asset_id."""
    spec = validate_spec(fixtures_dir / "valid_agent.yaml")
    register_asset(spec, fixtures_dir / "valid_agent.yaml", db_path=tmp_db)
    conn = get_connection(tmp_db)
    init_schema(conn)
    try:
        row = conn.execute(
            "SELECT id FROM assets WHERE name = ? AND version = ?",
            (spec.name, spec.version),
        ).fetchone()
        return str(row["id"])
    finally:
        conn.close()


def _seed_eval_sequence(tmp_db: Path, asset_id: str, observations: list[int]) -> None:
    """Insert ``observations`` (0/1) as chronologically-ordered eval_results rows."""
    conn = get_connection(tmp_db)
    init_schema(conn)
    base = datetime(2026, 6, 11, tzinfo=timezone.utc)
    try:
        for i, passed in enumerate(observations):
            run_at = (base + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
            conn.execute(
                """
                INSERT INTO eval_results
                    (id, asset_id, suite_name, passed, score_json, run_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    asset_id,
                    "basic_rca_suite",
                    int(passed),
                    json.dumps({"score": float(passed)}),
                    run_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Significance gate behaviour
# ---------------------------------------------------------------------------


class TestPromoteSignificanceGate:
    def test_noise_does_not_block(self, tmp_db: Path, fixtures_dir: Path) -> None:
        """A high pass-rate sequence (no regression) must NOT block promotion."""
        asset_id = _register_agent(tmp_db, fixtures_dir)
        # 30 runs, almost all passing → pass-rate ~0.97, holds H0 (acceptable).
        seq = [1] * 29 + [0]
        _seed_eval_sequence(tmp_db, asset_id, seq)

        with patch(
            "novafabric.registry.service.get_policy_engine", return_value=_allow_engine()
        ):
            result = promote_asset(
                "kube-rca-agent",
                "v1.0.0",
                AssetStatus.staging,
                actor="tester",
                significance_gate=True,
                db_path=tmp_db,
            )

        assert result["status"] == "staging"

    def test_significant_regression_blocks(
        self, tmp_db: Path, fixtures_dir: Path
    ) -> None:
        """A clear, sustained drop in pass-rate must block promotion."""
        asset_id = _register_agent(tmp_db, fixtures_dir)
        # 30 runs mostly failing → pass-rate ~0.13 << regression threshold → ACCEPT_H1.
        seq = [0] * 26 + [1] * 4
        _seed_eval_sequence(tmp_db, asset_id, seq)

        with patch(
            "novafabric.registry.service.get_policy_engine", return_value=_allow_engine()
        ):
            with pytest.raises(PromotionBlockedError) as exc_info:
                promote_asset(
                    "kube-rca-agent",
                    "v1.0.0",
                    AssetStatus.staging,
                    actor="tester",
                    significance_gate=True,
                    db_path=tmp_db,
                )

        assert "regression" in str(exc_info.value).lower()

    def test_inconclusive_does_not_block(
        self, tmp_db: Path, fixtures_dir: Path
    ) -> None:
        """Too few observations → CONTINUE → defer, do not block (fail-open on noise)."""
        asset_id = _register_agent(tmp_db, fixtures_dir)
        _seed_eval_sequence(tmp_db, asset_id, [1, 0, 1])

        with patch(
            "novafabric.registry.service.get_policy_engine", return_value=_allow_engine()
        ):
            result = promote_asset(
                "kube-rca-agent",
                "v1.0.0",
                AssetStatus.staging,
                actor="tester",
                significance_gate=True,
                db_path=tmp_db,
            )

        assert result["status"] == "staging"

    def test_gate_disabled_by_default(self, tmp_db: Path, fixtures_dir: Path) -> None:
        """Default (significance_gate=False) keeps legacy behaviour: a regression
        sequence does NOT block as long as there is one passing eval."""
        asset_id = _register_agent(tmp_db, fixtures_dir)
        seq = [0] * 26 + [1] * 4  # would be ACCEPT_H1 if the gate ran
        _seed_eval_sequence(tmp_db, asset_id, seq)

        with patch(
            "novafabric.registry.service.get_policy_engine", return_value=_allow_engine()
        ):
            result = promote_asset(
                "kube-rca-agent",
                "v1.0.0",
                AssetStatus.staging,
                actor="tester",
                db_path=tmp_db,
            )

        # Legacy single-score gate passes (one passing eval exists), gate ignored.
        assert result["status"] == "staging"

    def test_force_bypasses_significance_gate(
        self, tmp_db: Path, fixtures_dir: Path
    ) -> None:
        """force=True bypasses the significance gate even on a clear regression."""
        asset_id = _register_agent(tmp_db, fixtures_dir)
        _seed_eval_sequence(tmp_db, asset_id, [0] * 26 + [1] * 4)

        with patch(
            "novafabric.registry.service.get_policy_engine", return_value=_allow_engine()
        ):
            result = promote_asset(
                "kube-rca-agent",
                "v1.0.0",
                AssetStatus.staging,
                actor="tester",
                significance_gate=True,
                force=True,
                db_path=tmp_db,
            )

        assert result["status"] == "staging"
