"""ADR-0126 deployment-environment field: resolver, schema, capture, SDK.

Invariant under test everywhere: ABSENCE CHANGES NOTHING — a capsule without
the field is valid and byte-identical to today's format, and readers never
synthesize a value.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from novafabric.capture.deployment_env import (
    CONVENTIONAL_ENVIRONMENTS,
    ENVIRONMENT_ENV_VAR,
    InvalidDeploymentEnvironmentError,
    resolve_deployment_environment,
    unconventional_warning,
)
from novafabric.capture.orchestrator import CaptureOrchestrator

REPO_ROOT = Path(__file__).parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "capsule-environment"
SCHEMA_PATHS = [
    REPO_ROOT / "schemas" / "run-capsule.schema.json",
    REPO_ROOT / "src" / "novafabric" / "schemas" / "run-capsule.schema.json",
]


def _validator(schema_path: Path) -> jsonschema.Draft202012Validator:
    schema = json.loads(schema_path.read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


# ---------------------------------------------------------------------------
# Resolver: precedence, normalization, validation
# ---------------------------------------------------------------------------


class TestResolve:
    def test_no_source_resolves_to_none(self) -> None:
        assert resolve_deployment_environment(environ={}) is None

    def test_cli_flag_alone(self) -> None:
        resolved = resolve_deployment_environment(cli_value="production", environ={})
        assert resolved is not None
        assert resolved.value == "production"
        assert resolved.source == "cli-flag"

    def test_env_var_alone(self) -> None:
        resolved = resolve_deployment_environment(environ={ENVIRONMENT_ENV_VAR: "staging"})
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("staging", "env-var")

    def test_sdk_arg_alone(self) -> None:
        resolved = resolve_deployment_environment(sdk_value="test", environ={})
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("test", "sdk-arg")

    def test_cli_beats_env_var_and_sdk(self) -> None:
        resolved = resolve_deployment_environment(
            cli_value="production",
            sdk_value="development",
            environ={ENVIRONMENT_ENV_VAR: "staging"},
        )
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("production", "cli-flag")

    def test_env_var_beats_sdk(self) -> None:
        resolved = resolve_deployment_environment(
            sdk_value="development", environ={ENVIRONMENT_ENV_VAR: "staging"}
        )
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("staging", "env-var")

    def test_empty_cli_value_normalizes_to_absent_and_falls_through(self) -> None:
        resolved = resolve_deployment_environment(
            cli_value="", environ={ENVIRONMENT_ENV_VAR: "staging"}
        )
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("staging", "env-var")

    def test_all_sources_empty_resolves_to_none(self) -> None:
        assert (
            resolve_deployment_environment(
                cli_value="", sdk_value="  ", environ={ENVIRONMENT_ENV_VAR: ""}
            )
            is None
        )

    def test_custom_value_recorded_verbatim_case_preserving(self) -> None:
        resolved = resolve_deployment_environment(cli_value="Prod-EU.2026:a_b", environ={})
        assert resolved is not None
        assert resolved.value == "Prod-EU.2026:a_b"

    @pytest.mark.parametrize("bad", ["prod env", "prod/eu", "x" * 65, "é"])
    def test_invalid_cli_value_raises(self, bad: str) -> None:
        with pytest.raises(InvalidDeploymentEnvironmentError):
            resolve_deployment_environment(cli_value=bad, environ={})

    def test_invalid_sdk_value_raises(self) -> None:
        with pytest.raises(InvalidDeploymentEnvironmentError):
            resolve_deployment_environment(sdk_value="not valid", environ={})

    def test_invalid_env_var_warns_and_falls_through_to_sdk(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.deployment_env"):
            resolved = resolve_deployment_environment(
                sdk_value="test", environ={ENVIRONMENT_ENV_VAR: "bad value"}
            )
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("test", "sdk-arg")
        assert any(ENVIRONMENT_ENV_VAR in r.message for r in caplog.records)

    def test_invalid_env_var_alone_resolves_to_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.deployment_env"):
            assert (
                resolve_deployment_environment(environ={ENVIRONMENT_ENV_VAR: "bad value"})
                is None
            )

    def test_uses_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENVIRONMENT_ENV_VAR, "canary")
        resolved = resolve_deployment_environment()
        assert resolved is not None
        assert (resolved.value, resolved.source) == ("canary", "env-var")


class TestUnconventionalWarning:
    @pytest.mark.parametrize("value", sorted(CONVENTIONAL_ENVIRONMENTS))
    def test_conventional_values_do_not_warn(self, value: str) -> None:
        assert unconventional_warning(value) is None

    def test_check_is_case_insensitive(self) -> None:
        assert unconventional_warning("Production") is None
        assert unconventional_warning("STAGING") is None

    @pytest.mark.parametrize("value", ["prodction", "prod-eu", "canary"])
    def test_custom_or_typo_values_warn(self, value: str) -> None:
        warning = unconventional_warning(value)
        assert warning is not None
        assert value in warning


# ---------------------------------------------------------------------------
# Schema: golden fixtures against BOTH schema copies (root + packaged)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("schema_path", SCHEMA_PATHS, ids=["root", "packaged"])
@pytest.mark.parametrize(
    "fixture", sorted(p.name for p in FIXTURE_DIR.glob("*.json"))
)
def test_golden_fixture_behaves_as_named(schema_path: Path, fixture: str) -> None:
    manifest = json.loads((FIXTURE_DIR / fixture).read_text())
    errors = list(_validator(schema_path).iter_errors(manifest))
    if fixture.startswith("valid-"):
        assert errors == [], f"{fixture}: unexpected errors: {[e.message for e in errors]}"
    else:
        assert errors, f"{fixture}: expected schema rejection, got none"


def test_absent_fixture_carries_neither_field() -> None:
    """The backward-compat golden really is a pre-ADR-0126 manifest."""
    manifest = json.loads((FIXTURE_DIR / "valid-absent.json").read_text())
    assert "deployment_environment" not in manifest
    assert "environment_source" not in manifest


# ---------------------------------------------------------------------------
# Orchestrator capture: field recorded verbatim; absence changes nothing
# ---------------------------------------------------------------------------

PACKAGED_SCHEMA = SCHEMA_PATHS[1]


def _capture(tmp_path: Path, **run_kwargs: object) -> dict:  # type: ignore[type-arg]
    script = tmp_path / "agent.py"
    script.write_text("pass\n")
    orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
    result = orch.run(command=[sys.executable, str(script)], **run_kwargs)  # type: ignore[arg-type]
    manifest: dict = yaml.safe_load((result.capsule_dir / "capsule.yaml").read_text())  # type: ignore[type-arg]
    return manifest


class TestOrchestratorCapture:
    def test_environment_flag_recorded_with_cli_flag_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENVIRONMENT_ENV_VAR, raising=False)
        manifest = _capture(tmp_path, environment="production")
        assert manifest["deployment_environment"] == "production"
        assert manifest["environment_source"] == "cli-flag"
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_env_var_recorded_with_env_var_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENVIRONMENT_ENV_VAR, "prod-eu")
        manifest = _capture(tmp_path)
        assert manifest["deployment_environment"] == "prod-eu"
        assert manifest["environment_source"] == "env-var"
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_cli_flag_beats_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ENVIRONMENT_ENV_VAR, "staging")
        manifest = _capture(tmp_path, environment="production")
        assert manifest["deployment_environment"] == "production"
        assert manifest["environment_source"] == "cli-flag"

    def test_absent_by_default_and_still_schema_valid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENVIRONMENT_ENV_VAR, raising=False)
        manifest = _capture(tmp_path)
        assert "deployment_environment" not in manifest
        assert "environment_source" not in manifest
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_empty_flag_normalizes_to_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENVIRONMENT_ENV_VAR, raising=False)
        manifest = _capture(tmp_path, environment="")
        assert "deployment_environment" not in manifest
        assert "environment_source" not in manifest

    def test_invalid_flag_raises_before_any_capsule_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ENVIRONMENT_ENV_VAR, raising=False)
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        with pytest.raises(InvalidDeploymentEnvironmentError):
            orch.run(command=[sys.executable, "-c", "pass"], environment="not valid")
        assert list((tmp_path / "runs").iterdir()) == []

    def test_unconventional_value_warns_but_is_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.delenv(ENVIRONMENT_ENV_VAR, raising=False)
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.orchestrator"):
            manifest = _capture(tmp_path, environment="prodction")
        assert manifest["deployment_environment"] == "prodction"
        assert any("prodction" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# SDK decorator: sdk-arg source; env var takes precedence over the argument
# ---------------------------------------------------------------------------


class TestSdkCapture:
    def test_sdk_arg_recorded_with_sdk_arg_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.sdk.agent import agent

        monkeypatch.delenv(ENVIRONMENT_ENV_VAR, raising=False)
        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir, deployment_environment="test")
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert manifest["deployment_environment"] == "test"
        assert manifest["environment_source"] == "sdk-arg"

    def test_env_var_beats_sdk_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.sdk.agent import agent

        monkeypatch.setenv(ENVIRONMENT_ENV_VAR, "staging")
        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir, deployment_environment="test")
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert manifest["deployment_environment"] == "staging"
        assert manifest["environment_source"] == "env-var"

    def test_sdk_default_leaves_fields_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.sdk.agent import agent

        monkeypatch.delenv(ENVIRONMENT_ENV_VAR, raising=False)
        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir)
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert "deployment_environment" not in manifest
        assert "environment_source" not in manifest
