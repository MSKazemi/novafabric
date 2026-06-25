from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from novafabric.evals.suites.smoke import SmokeAdapter


@pytest.fixture
def smoke() -> SmokeAdapter:
    return SmokeAdapter()


def _make_capsule(tmp_path: Path, *, with_capsule_yaml: bool = True,
                  with_run_id: bool = True, with_replay_policy: bool = True) -> Path:
    """Create a minimal capsule directory for testing."""
    capsule_dir = tmp_path / "capsule"
    capsule_dir.mkdir()

    if with_capsule_yaml:
        data: dict[str, object] = {"schema_version": "0.1.0"}
        if with_run_id:
            data["run_id"] = "01TEST1234567890ABCDEFGHIJ"
        (capsule_dir / "capsule.yaml").write_text(yaml.dump(data))

    if with_replay_policy:
        (capsule_dir / "replay-policy.yaml").write_text("allow_readonly: true\n")

    return capsule_dir


def test_smoke_adapter_suite_id(smoke: SmokeAdapter) -> None:
    assert smoke.suite_id() == "novafabric-smoke-v1"


def test_smoke_adapter_oci_digest_is_empty(smoke: SmokeAdapter) -> None:
    assert smoke.oci_digest() == ""


def test_run_on_valid_capsule_returns_passed_true(tmp_path: Path, smoke: SmokeAdapter) -> None:
    capsule_dir = _make_capsule(tmp_path)
    result = smoke.run(capsule_dir, {})
    assert result.passed is True
    assert result.capsule_id == "01TEST1234567890ABCDEFGHIJ"
    assert all(m.passed for m in result.metrics)


def test_run_on_capsule_missing_capsule_yaml_returns_passed_false(
    tmp_path: Path, smoke: SmokeAdapter
) -> None:
    capsule_dir = _make_capsule(tmp_path, with_capsule_yaml=False)
    result = smoke.run(capsule_dir, {})
    assert result.passed is False
    schema_metric = next(m for m in result.metrics if m.name == "capsule_schema")
    assert schema_metric.passed is False
    assert result.capsule_id == "unknown"


def test_run_on_capsule_yaml_without_run_id_fails_capsule_schema(
    tmp_path: Path, smoke: SmokeAdapter
) -> None:
    capsule_dir = _make_capsule(tmp_path, with_run_id=False)
    result = smoke.run(capsule_dir, {})
    schema_metric = next(m for m in result.metrics if m.name == "capsule_schema")
    assert schema_metric.passed is False
    assert result.capsule_id == "unknown"
    # Overall must be False because capsule_schema failed
    assert result.passed is False


def test_run_on_capsule_without_replay_policy_fails_replay_metric(
    tmp_path: Path, smoke: SmokeAdapter
) -> None:
    capsule_dir = _make_capsule(tmp_path, with_replay_policy=False)
    result = smoke.run(capsule_dir, {})
    replay_metric = next(m for m in result.metrics if m.name == "replay_policy_present")
    assert replay_metric.passed is False
    assert result.passed is False


def test_run_returns_eval_result_with_two_metrics(tmp_path: Path, smoke: SmokeAdapter) -> None:
    capsule_dir = _make_capsule(tmp_path)
    result = smoke.run(capsule_dir, {})
    metric_names = {m.name for m in result.metrics}
    assert "capsule_schema" in metric_names
    assert "replay_policy_present" in metric_names


def test_run_capsule_id_from_run_id_field(tmp_path: Path, smoke: SmokeAdapter) -> None:
    capsule_dir = tmp_path / "cap"
    capsule_dir.mkdir()
    import yaml as _yaml
    (capsule_dir / "capsule.yaml").write_text(
        _yaml.dump({"run_id": "MY_CUSTOM_RUN_ID", "schema_version": "0.1.0"})
    )
    (capsule_dir / "replay-policy.yaml").write_text("allow_readonly: true\n")
    result = smoke.run(capsule_dir, {})
    assert result.capsule_id == "MY_CUSTOM_RUN_ID"
