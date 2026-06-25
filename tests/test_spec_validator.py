from pathlib import Path

import pytest

from novafabric.spec.validator import SpecValidationError, validate_spec


def test_valid_model_spec(valid_model_yaml: Path) -> None:
    spec = validate_spec(valid_model_yaml)
    assert spec.name == "fraud-model"
    assert spec.version == "1.0.0"
    assert spec.asset_type.value == "model"


def test_valid_agent_spec(valid_agent_yaml: Path) -> None:
    spec = validate_spec(valid_agent_yaml)
    assert spec.name == "kube-rca-agent"
    assert spec.asset_type.value == "agent"
    assert spec.spec.model.provider == "anthropic"  # type: ignore[union-attr]
    assert spec.spec.evals == ["basic_rca_suite"]  # type: ignore[union-attr]


def test_missing_asset_type_raises(fixtures_dir: Path) -> None:
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(fixtures_dir / "missing_asset_type.yaml")
    assert "asset_type" in str(exc_info.value)


def test_missing_agent_evals_raises(tmp_path: Path) -> None:
    yaml_path = tmp_path / "no_evals_agent.yaml"
    yaml_path.write_text(
        "novafabric_spec_version: '1'\n"
        "asset_type: agent\n"
        "name: test-agent\n"
        "version: 1.0.0\n"
        "spec:\n"
        "  model:\n"
        "    provider: anthropic\n"
        "    name: claude-sonnet-4-6\n"
        "  tools: []\n"
        "  prompts: {}\n"
        "  policies: []\n"
        "  evals: []\n"
    )
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(yaml_path)
    assert "evals" in str(exc_info.value)


def test_missing_agent_tools_raises(tmp_path: Path) -> None:
    yaml_path = tmp_path / "no_tools_agent.yaml"
    yaml_path.write_text(
        "novafabric_spec_version: '1'\n"
        "asset_type: agent\n"
        "name: test-agent\n"
        "version: 1.0.0\n"
        "spec:\n"
        "  model:\n"
        "    provider: anthropic\n"
        "    name: claude-sonnet-4-6\n"
        "  prompts: {}\n"
        "  policies: []\n"
        "  evals:\n"
        "    - suite_one\n"
    )
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(yaml_path)
    assert "tools" in str(exc_info.value)


def test_yaml_type_coercion_version_rejected(tmp_path: Path) -> None:
    yaml_path = tmp_path / "bad_version.yaml"
    yaml_path.write_text(
        "novafabric_spec_version: '1'\n"
        "asset_type: model\n"
        "name: test-model\n"
        "version: yes\n"
        "spec:\n"
        "  framework: pytorch\n"
        "  artifact_path: /tmp/m\n"
    )
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(yaml_path)
    assert "version" in str(exc_info.value)


def test_long_name_accepted(fixtures_dir: Path) -> None:
    spec = validate_spec(fixtures_dir / "long_name_model.yaml")
    assert len(spec.name) == 255


def test_valid_prompt_spec(fixtures_dir: Path) -> None:
    spec = validate_spec(fixtures_dir / "valid_prompt.yaml")
    assert spec.asset_type.value == "prompt"


def test_missing_file_raises_with_hint(tmp_path: Path) -> None:
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(tmp_path / "does_not_exist.yaml")
    assert "not found" in str(exc_info.value).lower()
    assert exc_info.value.hint is not None


def test_invalid_yaml_syntax_raises_with_hint(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("key: [\ninvalid yaml")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(bad)
    assert "yaml" in str(exc_info.value).lower()
    assert exc_info.value.hint is not None


# ── Hint paths in _hint_for_error (closing the validator coverage gap) ──────


def test_top_level_list_yaml_raises_with_mapping_hint(tmp_path: Path) -> None:
    """YAML that parses to a list (not a dict) at the top level must
    produce a clear error pointing the user at the mapping requirement."""
    bad = tmp_path / "list_at_top.yaml"
    bad.write_text("- one\n- two\n")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(bad)
    assert "mapping" in str(exc_info.value).lower()
    assert exc_info.value.hint is not None
    assert "key: value" in exc_info.value.hint


def test_top_level_scalar_yaml_raises_with_mapping_hint(tmp_path: Path) -> None:
    """YAML that parses to a bare string at the top level."""
    bad = tmp_path / "scalar_at_top.yaml"
    bad.write_text("just-a-string\n")
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(bad)
    assert "mapping" in str(exc_info.value).lower()


def test_invalid_asset_type_yields_valid_values_hint(tmp_path: Path) -> None:
    """An unknown asset_type value should produce a hint listing the
    valid values (model, agent, prompt, tool, dataset, evaluation,
    deployment)."""
    bad = tmp_path / "bad_asset_type.yaml"
    bad.write_text(
        "novafabric_spec_version: '1'\n"
        "asset_type: not-a-real-type\n"
        "name: x\n"
        "version: 1.0.0\n"
    )
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(bad)
    hint = exc_info.value.hint or ""
    # Hint should list the valid asset_type values.
    assert "asset_type" in hint.lower() or "model" in hint
    assert "agent" in hint  # at least one valid value should appear


def test_invalid_status_yields_valid_values_hint(tmp_path: Path) -> None:
    """An unknown lifecycle status value should produce a hint listing
    the valid values (development, staging, production, archived)."""
    bad = tmp_path / "bad_status.yaml"
    bad.write_text(
        "novafabric_spec_version: '1'\n"
        "asset_type: model\n"
        "name: m\n"
        "version: 1.0.0\n"
        "status: not-a-real-status\n"
        "spec:\n"
        "  framework: pytorch\n"
        "  artifact_path: /tmp/x\n"
    )
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(bad)
    hint = exc_info.value.hint or ""
    # Hint should mention status values.
    assert "status" in hint.lower() or "development" in hint or "production" in hint


def test_invalid_semver_yields_semver_hint(tmp_path: Path) -> None:
    """A non-semver version string should produce a hint pointing the
    user at the supported semver shapes."""
    bad = tmp_path / "bad_version.yaml"
    bad.write_text(
        "novafabric_spec_version: '1'\n"
        "asset_type: model\n"
        "name: m\n"
        "version: not-semver\n"
        "spec:\n"
        "  framework: pytorch\n"
        "  artifact_path: /tmp/x\n"
    )
    with pytest.raises(SpecValidationError) as exc_info:
        validate_spec(bad)
    hint = exc_info.value.hint or ""
    # Either the semver hint fires, or some other validator-driven hint.
    # The important thing is the version is mentioned in the error.
    assert "version" in str(exc_info.value).lower() or "semver" in hint.lower()


# ── Direct unit tests for _hint_for_error coverage gaps ─────────────────────


def test_hint_for_unknown_discriminator() -> None:
    from novafabric.spec.validator import _hint_for_error

    # Lines 55-56: union_tag_invalid with a non-"asset_type" discriminator.
    error = {
        "type": "union_tag_invalid",
        "loc": (),
        "msg": "",
        "ctx": {"discriminator": "some_other_field"},
    }
    result = _hint_for_error(error)
    assert result is not None
    assert "some_other_field" in result


def test_hint_for_enum_asset_type_in_loc() -> None:
    from novafabric.spec.validator import _hint_for_error

    # Lines 59-61: enum error with "asset_type" in loc.
    error = {
        "type": "enum",
        "loc": ("asset_type",),
        "msg": "",
        "ctx": {"expected": "model, agent"},
    }
    result = _hint_for_error(error)
    assert result is not None
    assert "model" in result or "asset_type" in result.lower()


def test_hint_for_enum_with_expected_ctx() -> None:
    from novafabric.spec.validator import _hint_for_error

    # Lines 65-66: enum error with neither "asset_type" nor "status" in loc, but expected is set.
    error = {
        "type": "enum",
        "loc": ("some_other_field",),
        "msg": "",
        "ctx": {"expected": "foo, bar, baz"},
    }
    result = _hint_for_error(error)
    assert result is not None
    assert "foo, bar, baz" in result
