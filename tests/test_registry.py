from pathlib import Path

import pytest

from novafabric.registry.service import (
    AssetNotFoundError,
    DuplicateAssetError,
    InvalidLifecycleTransitionError,
    PromotionBlockedError,
    get_asset,
    list_assets,
    list_assets_paginated,
    promote_asset,
    register_asset,
)
from novafabric.registry.store import get_connection, init_schema
from novafabric.spec.models import AssetStatus
from novafabric.spec.validator import validate_spec


def test_init_schema_creates_tables(tmp_db: Path) -> None:
    conn = get_connection(tmp_db)
    init_schema(conn)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "assets" in tables
    assert "eval_results" in tables
    assert "schema_version" in tables
    conn.close()


def test_init_schema_is_idempotent(tmp_db: Path) -> None:
    conn = get_connection(tmp_db)
    init_schema(conn)
    init_schema(conn)  # second call must not raise
    conn.close()


def test_wal_mode_enabled(tmp_db: Path) -> None:
    conn = get_connection(tmp_db)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal"
    conn.close()


# --- Service tests (Task 5) ---


def test_register_valid_asset(tmp_db: Path, valid_model_yaml: Path) -> None:
    spec = validate_spec(valid_model_yaml)
    result = register_asset(spec, valid_model_yaml, db_path=tmp_db)
    assert "id" in result
    assert result["name"] == "fraud-model"
    assert result["version"] == "1.0.0"
    assert result["status"] == "development"


def test_register_duplicate_raises(tmp_db: Path, valid_model_yaml: Path) -> None:
    spec = validate_spec(valid_model_yaml)
    register_asset(spec, valid_model_yaml, db_path=tmp_db)
    with pytest.raises(DuplicateAssetError):
        register_asset(spec, valid_model_yaml, db_path=tmp_db)


def test_list_with_type_filter(tmp_db: Path, fixtures_dir: Path) -> None:
    model_spec = validate_spec(fixtures_dir / "valid_model.yaml")
    agent_spec = validate_spec(fixtures_dir / "valid_agent.yaml")
    register_asset(model_spec, fixtures_dir / "valid_model.yaml", db_path=tmp_db)
    register_asset(agent_spec, fixtures_dir / "valid_agent.yaml", db_path=tmp_db)

    models = list_assets(asset_type="model", status=None, db_path=tmp_db)
    assert len(models) == 1
    assert models[0]["name"] == "fraud-model"

    all_assets = list_assets(asset_type=None, status=None, db_path=tmp_db)
    assert len(all_assets) == 2


def _seed_assets(db_path: Path, n: int) -> None:
    """Insert ``n`` synthetic asset rows with monotonically increasing
    created_at so DESC ordering is deterministic (newest = highest index)."""
    conn = get_connection(db_path)
    init_schema(conn)
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO assets (id, name, asset_type, version, status, "
                "spec_json, git_commit_sha, created_at, promoted_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    f"id-{i:03d}",
                    f"asset-{i:03d}",
                    "model" if i % 2 == 0 else "agent",
                    "1.0.0",
                    "development",
                    '{"big": "spec blob that should NOT appear in list view"}',
                    "deadbeef",
                    f"2026-05-01T00:{i:02d}:00Z",
                    None,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_list_assets_paginated_returns_page_and_total(tmp_db: Path) -> None:
    _seed_assets(tmp_db, 25)
    page, total = list_assets_paginated(None, None, limit=10, offset=0, db_path=tmp_db)
    assert total == 25
    assert len(page) == 10
    # Newest-first ordering: highest created_at (index 24) is first.
    assert page[0]["name"] == "asset-024"


def test_list_assets_paginated_offset_window(tmp_db: Path) -> None:
    _seed_assets(tmp_db, 25)
    page, total = list_assets_paginated(None, None, limit=10, offset=20, db_path=tmp_db)
    assert total == 25
    assert len(page) == 5  # only 5 rows remain after offset 20
    # offset 20 in DESC order = the 21st-newest = index 4
    assert page[0]["name"] == "asset-004"


def test_list_assets_paginated_omits_spec_json(tmp_db: Path) -> None:
    _seed_assets(tmp_db, 3)
    page, _ = list_assets_paginated(None, None, limit=10, offset=0, db_path=tmp_db)
    assert page, "expected rows"
    assert "spec_json" not in page[0]
    # but the list-view columns are present
    for col in (
        "id", "name", "version", "asset_type", "status",
        "created_at", "promoted_at", "git_commit_sha",
    ):
        assert col in page[0]


def test_list_assets_paginated_type_filter_counts_filtered_total(tmp_db: Path) -> None:
    _seed_assets(tmp_db, 10)  # 5 model, 5 agent
    page, total = list_assets_paginated(
        "model", None, limit=100, offset=0, db_path=tmp_db
    )
    assert total == 5
    assert all(r["asset_type"] == "model" for r in page)


def test_list_assets_paginated_has_more_semantics(tmp_db: Path) -> None:
    _seed_assets(tmp_db, 12)
    _, total = list_assets_paginated(None, None, limit=5, offset=0, db_path=tmp_db)
    # endpoint computes has_more = offset + limit < total
    assert (0 + 5 < total) is True
    assert (10 + 5 < total) is False


def test_get_asset_by_name_and_version(tmp_db: Path, valid_model_yaml: Path) -> None:
    spec = validate_spec(valid_model_yaml)
    register_asset(spec, valid_model_yaml, db_path=tmp_db)
    asset = get_asset("fraud-model", "1.0.0", db_path=tmp_db)
    assert asset["name"] == "fraud-model"


def test_get_asset_without_version_returns_latest(
    tmp_db: Path, valid_model_yaml: Path, fixtures_dir: Path
) -> None:
    spec = validate_spec(valid_model_yaml)
    register_asset(spec, valid_model_yaml, db_path=tmp_db)

    spec2_yaml = fixtures_dir / "valid_model.yaml"
    spec2 = validate_spec(spec2_yaml)
    spec2_modified = spec2.model_copy(update={"version": "2.0.0"})
    register_asset(spec2_modified, spec2_yaml, db_path=tmp_db)

    asset = get_asset("fraud-model", None, db_path=tmp_db)
    assert asset["version"] == "2.0.0"


def test_get_asset_not_found_raises(tmp_db: Path) -> None:
    with pytest.raises(AssetNotFoundError):
        get_asset("nonexistent", "1.0.0", db_path=tmp_db)


def test_promote_model_lifecycle(tmp_db: Path, valid_model_yaml: Path) -> None:
    spec = validate_spec(valid_model_yaml)
    register_asset(spec, valid_model_yaml, db_path=tmp_db)
    result = promote_asset(
        "fraud-model", "1.0.0", AssetStatus.staging, "test-user", db_path=tmp_db
    )
    assert result["status"] == "staging"
    assert result["promoted_at"] is not None


def test_promote_invalid_transition_raises(
    tmp_db: Path, valid_model_yaml: Path
) -> None:
    spec = validate_spec(valid_model_yaml)
    register_asset(spec, valid_model_yaml, db_path=tmp_db)
    promote_asset(
        "fraud-model", "1.0.0", AssetStatus.staging, "test-user", db_path=tmp_db
    )
    promote_asset(
        "fraud-model", "1.0.0", AssetStatus.production, "test-user", db_path=tmp_db
    )
    with pytest.raises(InvalidLifecycleTransitionError):
        promote_asset(
            "fraud-model",
            "1.0.0",
            AssetStatus.development,
            "test-user",
            db_path=tmp_db,
        )


def test_promote_agent_without_eval_raises(
    tmp_db: Path, valid_agent_yaml: Path
) -> None:
    spec = validate_spec(valid_agent_yaml)
    register_asset(spec, valid_agent_yaml, db_path=tmp_db)
    with pytest.raises(PromotionBlockedError):
        promote_asset(
            "kube-rca-agent",
            "v1.0.0",
            AssetStatus.staging,
            "test-user",
            db_path=tmp_db,
        )


def test_promote_agent_with_force_records_flag(
    tmp_db: Path, valid_agent_yaml: Path
) -> None:
    spec = validate_spec(valid_agent_yaml)
    register_asset(spec, valid_agent_yaml, db_path=tmp_db)
    result = promote_asset(
        "kube-rca-agent",
        "v1.0.0",
        AssetStatus.staging,
        "test-user",
        force=True,
        db_path=tmp_db,
    )
    assert result["forced_promotion"] is True
    assert result["status"] == "staging"
