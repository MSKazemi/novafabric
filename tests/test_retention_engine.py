from pathlib import Path

from novafabric.storage._retention import LegalHold, RetentionPolicy, RetentionPolicyEngine


def test_effective_retention_no_holds() -> None:
    policy = RetentionPolicy(registry="r", retention_days=365)
    engine = RetentionPolicyEngine()
    assert engine.effective_retention(policy) == 365


def test_effective_retention_with_longer_hold() -> None:
    policy = RetentionPolicy(registry="r", retention_days=365, legal_hold_ids=["h1"])
    engine = RetentionPolicyEngine(
        holds=[LegalHold(hold_id="h1", reason="exam", duration_days=730)]
    )
    assert engine.effective_retention(policy) == 730


def test_effective_retention_hold_shorter_than_policy() -> None:
    # Hold is shorter than policy — policy wins
    policy = RetentionPolicy(registry="r", retention_days=365, legal_hold_ids=["h1"])
    engine = RetentionPolicyEngine(
        holds=[LegalHold(hold_id="h1", reason="exam", duration_days=30)]
    )
    assert engine.effective_retention(policy) == 365


def test_effective_retention_indefinite_hold() -> None:
    policy = RetentionPolicy(registry="r", retention_days=365, legal_hold_ids=["h1"])
    engine = RetentionPolicyEngine(
        holds=[LegalHold(hold_id="h1", reason="exam", duration_days=None)]
    )
    assert engine.effective_retention(policy) > 10**8


def test_can_delete_within_window() -> None:
    policy = RetentionPolicy(registry="r", retention_days=365)
    engine = RetentionPolicyEngine()
    allowed, reason = engine.can_delete(policy, age_days=100)
    assert not allowed
    assert "100" in reason and "365" in reason


def test_can_delete_after_window() -> None:
    policy = RetentionPolicy(registry="r", retention_days=30)
    engine = RetentionPolicyEngine()
    allowed, reason = engine.can_delete(policy, age_days=31)
    assert allowed
    assert reason == ""


def test_cannot_delete_prohibited() -> None:
    policy = RetentionPolicy(registry="r", retention_days=30, deletion_mode="prohibited")
    engine = RetentionPolicyEngine()
    allowed, reason = engine.can_delete(policy, age_days=9999)
    assert not allowed
    assert "prohibited" in reason


def test_cannot_delete_with_active_hold() -> None:
    policy = RetentionPolicy(registry="r", retention_days=30, legal_hold_ids=["h1"])
    engine = RetentionPolicyEngine(
        holds=[LegalHold(hold_id="h1", reason="litigation", duration_days=None)]
    )
    allowed, reason = engine.can_delete(policy, age_days=9999)
    assert not allowed
    assert "h1" in reason


def test_can_delete_with_unresolved_hold_id() -> None:
    """A hold_id referenced in policy but not in engine._holds should not block deletion."""
    policy = RetentionPolicy(registry="r", retention_days=30, legal_hold_ids=["h1"])
    engine = RetentionPolicyEngine(holds=[])  # h1 is not in the engine
    allowed, reason = engine.can_delete(policy, age_days=31)
    assert allowed, f"Expected deletion allowed but got: {reason}"


def test_from_yaml(tmp_path: Path) -> None:
    yaml_content = """
version: 1
registry: trading-registry
jurisdiction: eu/mifid-ii
retention_days: 1825
deletion_mode: defensible
legal_hold_ids: []
"""
    path = tmp_path / "retention-policy.yaml"
    path.write_text(yaml_content)
    policy = RetentionPolicy.from_yaml(path)
    assert policy.retention_days == 1825
    assert policy.registry == "trading-registry"
    assert policy.deletion_mode == "defensible"
    assert policy.legal_hold_ids == []
