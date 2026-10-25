"""The agent promote gate needs the most recent eval to pass (review replay-lineage-registry-13).

``_has_passing_eval`` accepted ANY historical pass: an agent that passed once and
then failed its last ten evals still cleared the gate to staging/production.
With the opt-in significance gate the whole sequence is judged instead (ADR-0080).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from test_promote_significance_gate import _allow_engine, _register_agent, _seed_eval_sequence

from novafabric.registry.service import PromotionBlockedError, promote_asset
from novafabric.spec.models import AssetStatus


def _promote(tmp_db: Path, **kw: object) -> dict:
    with patch("novafabric.registry.service.get_policy_engine", return_value=_allow_engine()):
        return promote_asset(
            "kube-rca-agent", "v1.0.0", AssetStatus.staging, actor="tester",
            db_path=tmp_db, **kw,  # type: ignore[arg-type]
        )


def test_an_old_pass_does_not_clear_a_failing_latest_eval(tmp_db: Path, fixtures_dir: Path) -> None:
    asset_id = _register_agent(tmp_db, fixtures_dir)
    _seed_eval_sequence(tmp_db, asset_id, [1] + [0] * 10)
    with pytest.raises(PromotionBlockedError, match="most recent evaluation must pass"):
        _promote(tmp_db)


def test_a_latest_pass_clears_it_whatever_came_before(tmp_db: Path, fixtures_dir: Path) -> None:
    asset_id = _register_agent(tmp_db, fixtures_dir)
    _seed_eval_sequence(tmp_db, asset_id, [0] * 10 + [1])
    assert _promote(tmp_db)["status"] == "staging"


def test_no_eval_at_all_is_still_blocked(tmp_db: Path, fixtures_dir: Path) -> None:
    _register_agent(tmp_db, fixtures_dir)
    with pytest.raises(PromotionBlockedError, match="no passing evaluation"):
        _promote(tmp_db)


def test_force_still_overrides(tmp_db: Path, fixtures_dir: Path) -> None:
    asset_id = _register_agent(tmp_db, fixtures_dir)
    _seed_eval_sequence(tmp_db, asset_id, [1, 0])
    assert _promote(tmp_db, force=True)["status"] == "staging"


def test_the_significance_gate_keeps_judging_the_whole_sequence(
    tmp_db: Path, fixtures_dir: Path
) -> None:
    # 29 passes then one failure is noise for the SPRT: it must not block (ADR-0080).
    asset_id = _register_agent(tmp_db, fixtures_dir)
    _seed_eval_sequence(tmp_db, asset_id, [1] * 29 + [0])
    assert _promote(tmp_db, significance_gate=True)["status"] == "staging"
