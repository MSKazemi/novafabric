"""ADR-0116 variant-attribution field: resolver, schema, capture, SDK.

Invariants under test everywhere:

- ABSENCE CHANGES NOTHING — a capsule without the ``variant`` block is valid
  and identical to today's format; readers never synthesize a value.
- RECORD-ONLY — every field is copied verbatim from the caller; NovaFabric
  never allocates, derives, or defaults any of them (in particular
  ``assignment_source`` and ``assigned_at``).
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.capture.variant import (
    EXPERIMENT_ENV_VAR,
    VARIANT_ASSIGNED_AT_ENV_VAR,
    VARIANT_ENV_VAR,
    VARIANT_LABEL_ENV_VAR,
    VARIANT_SOURCE_ENV_VAR,
    InvalidVariantAttributionError,
    resolve_variant_attribution,
)

REPO_ROOT = Path(__file__).parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "capsule-variant"
SCHEMA_PATHS = [
    REPO_ROOT / "schemas" / "run-capsule.schema.json",
    REPO_ROOT / "src" / "novafabric" / "schemas" / "run-capsule.schema.json",
]

_CLI_TRIPLE = {
    "experiment_id": "exp-1",
    "variant_id": "arm-a",
    "assignment_source": "launchdarkly",
}
_ENV_TRIPLE = {
    EXPERIMENT_ENV_VAR: "exp-env",
    VARIANT_ENV_VAR: "arm-env",
    VARIANT_SOURCE_ENV_VAR: "statsig",
}
_SDK_TRIPLE = {
    "experiment_id": "exp-sdk",
    "variant_id": "arm-sdk",
    "assignment_source": "upstream-router",
}


def _validator(schema_path: Path) -> jsonschema.Draft202012Validator:
    schema = json.loads(schema_path.read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


# ---------------------------------------------------------------------------
# Resolver: precedence (atomic per tier), verbatim copy, validation
# ---------------------------------------------------------------------------


class TestResolve:
    def test_no_source_resolves_to_none(self) -> None:
        assert resolve_variant_attribution(environ={}) is None

    def test_cli_tier_alone(self) -> None:
        resolved = resolve_variant_attribution(cli_values=_CLI_TRIPLE, environ={})
        assert resolved is not None
        assert resolved.experiment_id == "exp-1"
        assert resolved.variant_id == "arm-a"
        assert resolved.assignment_source == "launchdarkly"
        assert resolved.variant_label is None
        assert resolved.assigned_at is None

    def test_env_tier_alone(self) -> None:
        resolved = resolve_variant_attribution(environ=dict(_ENV_TRIPLE))
        assert resolved is not None
        assert (resolved.experiment_id, resolved.variant_id) == ("exp-env", "arm-env")
        assert resolved.assignment_source == "statsig"

    def test_sdk_tier_alone(self) -> None:
        resolved = resolve_variant_attribution(sdk_values=_SDK_TRIPLE, environ={})
        assert resolved is not None
        assert (resolved.experiment_id, resolved.variant_id) == ("exp-sdk", "arm-sdk")

    def test_cli_beats_env_and_sdk_atomically(self) -> None:
        resolved = resolve_variant_attribution(
            cli_values=_CLI_TRIPLE, sdk_values=_SDK_TRIPLE, environ=dict(_ENV_TRIPLE)
        )
        assert resolved is not None
        # The whole block comes from the CLI tier — no cross-tier mixing.
        assert (resolved.experiment_id, resolved.variant_id, resolved.assignment_source) == (
            "exp-1", "arm-a", "launchdarkly"
        )

    def test_env_beats_sdk(self) -> None:
        resolved = resolve_variant_attribution(
            sdk_values=_SDK_TRIPLE, environ=dict(_ENV_TRIPLE)
        )
        assert resolved is not None
        assert (resolved.experiment_id, resolved.variant_id) == ("exp-env", "arm-env")

    def test_optional_fields_recorded_verbatim(self) -> None:
        resolved = resolve_variant_attribution(
            cli_values={
                **_CLI_TRIPLE,
                "variant_label": "control",
                "assigned_at": "2026-07-12T09:14:03.221Z",
            },
            environ={},
        )
        assert resolved is not None
        assert resolved.variant_label == "control"
        assert resolved.assigned_at == "2026-07-12T09:14:03.221Z"

    def test_empty_values_normalize_to_absent(self) -> None:
        assert (
            resolve_variant_attribution(
                cli_values={"experiment_id": "", "variant_id": "  ", "assignment_source": None},
                environ={VARIANT_ENV_VAR: ""},
            )
            is None
        )

    @pytest.mark.parametrize(
        "partial",
        [
            {"variant_id": "arm-a", "assignment_source": "x"},
            {"experiment_id": "exp-1", "assignment_source": "x"},
            {"experiment_id": "exp-1", "variant_id": "arm-a"},  # no source — never defaulted
            {"variant_label": "control"},
        ],
        ids=["no-experiment", "no-variant", "no-source", "label-only"],
    )
    def test_incomplete_cli_tier_raises(self, partial: dict[str, str]) -> None:
        with pytest.raises(InvalidVariantAttributionError, match="missing"):
            resolve_variant_attribution(cli_values=partial, environ={})

    def test_incomplete_sdk_tier_raises(self) -> None:
        with pytest.raises(InvalidVariantAttributionError):
            resolve_variant_attribution(
                sdk_values={"experiment_id": "exp-1", "variant_id": "arm-a"}, environ={}
            )

    def test_unknown_sdk_key_raises(self) -> None:
        with pytest.raises(InvalidVariantAttributionError, match="unknown"):
            resolve_variant_attribution(
                sdk_values={**_SDK_TRIPLE, "traffic_split": 0.5}, environ={}
            )

    def test_non_mapping_extensions_raises(self) -> None:
        with pytest.raises(InvalidVariantAttributionError, match="extensions"):
            resolve_variant_attribution(
                sdk_values={**_SDK_TRIPLE, "extensions": "io.x=1"}, environ={}
            )

    def test_sdk_extensions_carried_verbatim(self) -> None:
        resolved = resolve_variant_attribution(
            sdk_values={**_SDK_TRIPLE, "extensions": {"io.launchdarkly.flag_key": "f"}},
            environ={},
        )
        assert resolved is not None
        assert resolved.extensions == {"io.launchdarkly.flag_key": "f"}

    def test_incomplete_env_tier_warns_and_falls_through_to_sdk(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.variant"):
            resolved = resolve_variant_attribution(
                sdk_values=_SDK_TRIPLE, environ={VARIANT_ENV_VAR: "arm-env"}
            )
        assert resolved is not None
        assert resolved.variant_id == "arm-sdk"
        assert any("missing" in r.message for r in caplog.records)

    def test_incomplete_env_tier_alone_resolves_to_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.variant"):
            assert resolve_variant_attribution(environ={VARIANT_ENV_VAR: "arm-env"}) is None

    def test_invalid_explicit_assigned_at_raises(self) -> None:
        with pytest.raises(InvalidVariantAttributionError, match="assigned_at"):
            resolve_variant_attribution(
                cli_values={**_CLI_TRIPLE, "assigned_at": "yesterday"}, environ={}
            )

    def test_naive_assigned_at_rejected(self) -> None:
        with pytest.raises(InvalidVariantAttributionError, match="assigned_at"):
            resolve_variant_attribution(
                cli_values={**_CLI_TRIPLE, "assigned_at": "2026-07-12T09:14:03"}, environ={}
            )

    def test_invalid_env_assigned_at_drops_only_the_timestamp(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.variant"):
            resolved = resolve_variant_attribution(
                environ={**_ENV_TRIPLE, VARIANT_ASSIGNED_AT_ENV_VAR: "not-a-time"}
            )
        assert resolved is not None
        assert resolved.assigned_at is None  # bad ambient timestamp dropped, arm kept
        assert resolved.variant_id == "arm-env"

    def test_env_optional_fields_recorded(self) -> None:
        resolved = resolve_variant_attribution(
            environ={
                **_ENV_TRIPLE,
                VARIANT_LABEL_ENV_VAR: "control",
                VARIANT_ASSIGNED_AT_ENV_VAR: "2026-07-12T09:14:03+00:00",
            }
        )
        assert resolved is not None
        assert resolved.variant_label == "control"
        assert resolved.assigned_at == "2026-07-12T09:14:03+00:00"

    def test_uses_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for var, value in _ENV_TRIPLE.items():
            monkeypatch.setenv(var, value)
        resolved = resolve_variant_attribution()
        assert resolved is not None
        assert resolved.variant_id == "arm-env"


class TestManifestBlock:
    def test_minimal_block_omits_absent_optionals(self) -> None:
        resolved = resolve_variant_attribution(cli_values=_CLI_TRIPLE, environ={})
        assert resolved is not None
        assert resolved.to_manifest_block() == {
            "experiment_id": "exp-1",
            "variant_id": "arm-a",
            "assignment_source": "launchdarkly",
        }

    def test_full_block_round_trips(self) -> None:
        resolved = resolve_variant_attribution(
            sdk_values={
                **_SDK_TRIPLE,
                "variant_label": "control",
                "assigned_at": "2026-07-12T09:14:03.221Z",
                "extensions": {"io.example.k": "v"},
            },
            environ={},
        )
        assert resolved is not None
        block = resolved.to_manifest_block()
        assert block["variant_label"] == "control"
        assert block["assigned_at"] == "2026-07-12T09:14:03.221Z"
        assert block["extensions"] == {"io.example.k": "v"}


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


def test_absent_fixture_carries_no_variant_block() -> None:
    """The backward-compat golden really is a pre-ADR-0116 manifest."""
    manifest = json.loads((FIXTURE_DIR / "valid-absent.json").read_text())
    assert "variant" not in manifest


# ---------------------------------------------------------------------------
# Orchestrator capture: block recorded verbatim; absence changes nothing
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
    def test_flags_recorded_verbatim(self, tmp_path: Path) -> None:
        manifest = _capture(
            tmp_path,
            experiment="exp-1",
            variant="arm-a",
            variant_source="launchdarkly",
            variant_label="control",
            variant_assigned_at="2026-07-12T09:14:03.221Z",
        )
        assert manifest["variant"] == {
            "experiment_id": "exp-1",
            "variant_id": "arm-a",
            "assignment_source": "launchdarkly",
            "variant_label": "control",
            "assigned_at": "2026-07-12T09:14:03.221Z",
        }
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_env_vars_recorded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for var, value in _ENV_TRIPLE.items():
            monkeypatch.setenv(var, value)
        manifest = _capture(tmp_path)
        assert manifest["variant"]["variant_id"] == "arm-env"
        assert manifest["variant"]["assignment_source"] == "statsig"
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_cli_flags_beat_env_vars(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var, value in _ENV_TRIPLE.items():
            monkeypatch.setenv(var, value)
        manifest = _capture(
            tmp_path, experiment="exp-1", variant="arm-a", variant_source="launchdarkly"
        )
        assert manifest["variant"]["variant_id"] == "arm-a"
        assert manifest["variant"]["experiment_id"] == "exp-1"

    def test_absent_by_default_and_still_schema_valid(self, tmp_path: Path) -> None:
        manifest = _capture(tmp_path)
        assert "variant" not in manifest
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_incomplete_flags_raise_before_any_capsule_is_written(
        self, tmp_path: Path
    ) -> None:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        with pytest.raises(InvalidVariantAttributionError):
            # --variant without --experiment/--variant-source: never defaulted.
            orch.run(command=[sys.executable, "-c", "pass"], variant="arm-a")
        assert list((tmp_path / "runs").iterdir()) == []


# ---------------------------------------------------------------------------
# SDK decorator: sdk-arg tier; env vars take precedence over the argument
# ---------------------------------------------------------------------------


class TestSdkCapture:
    def test_sdk_variant_mapping_recorded(self, tmp_path: Path) -> None:
        from novafabric.sdk.agent import agent

        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir, variant=_SDK_TRIPLE)
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert manifest["variant"] == {
            "experiment_id": "exp-sdk",
            "variant_id": "arm-sdk",
            "assignment_source": "upstream-router",
        }

    def test_env_vars_beat_sdk_arg(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.sdk.agent import agent

        for var, value in _ENV_TRIPLE.items():
            monkeypatch.setenv(var, value)
        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir, variant=_SDK_TRIPLE)
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert manifest["variant"]["variant_id"] == "arm-env"

    def test_sdk_default_leaves_block_absent(self, tmp_path: Path) -> None:
        from novafabric.sdk.agent import agent

        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir)
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert "variant" not in manifest

    def test_incomplete_sdk_variant_raises_before_workload_runs(
        self, tmp_path: Path
    ) -> None:
        from novafabric.sdk.agent import agent

        ran = False

        @agent(name="a", version="1.0", capsule_dir=tmp_path / "capsule",
               variant={"variant_id": "arm-a"})
        def noop() -> None:
            nonlocal ran
            ran = True

        with pytest.raises(InvalidVariantAttributionError):
            noop()
        assert ran is False
