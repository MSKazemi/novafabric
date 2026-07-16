"""ADR-0122 session back-reference on capsules: resolver, schema, capture, SDK.

Invariant under test everywhere: ABSENCE CHANGES NOTHING — a capsule without
``session_id``/``sequence`` is a standalone run, valid and byte-identical to
today's format, and readers never synthesize a membership.
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
from novafabric.capture.session import (
    SESSION_ID_ENV_VAR,
    SESSION_SEQUENCE_ENV_VAR,
    InvalidSessionMembershipError,
    resolve_session_membership,
)

REPO_ROOT = Path(__file__).parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "capsule-session"
SCHEMA_PATHS = [
    REPO_ROOT / "schemas" / "run-capsule.schema.json",
    REPO_ROOT / "src" / "novafabric" / "schemas" / "run-capsule.schema.json",
]

SID = "01HZ8S9K3M4YZ2K7N9DPBYK2W0"
OTHER_SID = "01HZ8T0A00YZ2K7N9DPBYK2W01"


def _validator(schema_path: Path) -> jsonschema.Draft202012Validator:
    schema = json.loads(schema_path.read_text())
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


# ---------------------------------------------------------------------------
# Resolver: precedence, atomic-per-tier resolution, validation
# ---------------------------------------------------------------------------


class TestResolve:
    def test_no_source_resolves_to_none(self) -> None:
        assert resolve_session_membership(environ={}) is None

    def test_cli_flags_alone(self) -> None:
        resolved = resolve_session_membership(
            cli_session_id=SID, cli_sequence=2, environ={}
        )
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence, resolved.source) == (
            SID,
            2,
            "cli-flag",
        )

    def test_session_id_alone_is_valid_sequence_stays_absent(self) -> None:
        resolved = resolve_session_membership(cli_session_id=SID, environ={})
        assert resolved is not None
        assert resolved.sequence is None

    def test_env_vars_alone(self) -> None:
        resolved = resolve_session_membership(
            environ={SESSION_ID_ENV_VAR: SID, SESSION_SEQUENCE_ENV_VAR: "3"}
        )
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence, resolved.source) == (
            SID,
            3,
            "env-var",
        )

    def test_sdk_args_alone(self) -> None:
        resolved = resolve_session_membership(
            sdk_session_id=SID, sdk_sequence=0, environ={}
        )
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence, resolved.source) == (
            SID,
            0,
            "sdk-arg",
        )

    def test_cli_beats_env_and_sdk(self) -> None:
        resolved = resolve_session_membership(
            cli_session_id=SID,
            cli_sequence=1,
            sdk_session_id=OTHER_SID,
            sdk_sequence=9,
            environ={SESSION_ID_ENV_VAR: OTHER_SID, SESSION_SEQUENCE_ENV_VAR: "8"},
        )
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence, resolved.source) == (
            SID,
            1,
            "cli-flag",
        )

    def test_env_beats_sdk(self) -> None:
        resolved = resolve_session_membership(
            sdk_session_id=OTHER_SID,
            sdk_sequence=9,
            environ={SESSION_ID_ENV_VAR: SID},
        )
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence, resolved.source) == (
            SID,
            None,
            "env-var",
        )

    def test_winning_tier_is_atomic_no_cross_tier_sequence(self) -> None:
        """A session id from one tier never picks up a sequence from another."""
        resolved = resolve_session_membership(
            cli_session_id=SID,
            environ={SESSION_SEQUENCE_ENV_VAR: "7"},
        )
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence) == (SID, None)

    def test_empty_values_normalize_to_absent(self) -> None:
        assert (
            resolve_session_membership(
                cli_session_id="  ",
                environ={SESSION_ID_ENV_VAR: "", SESSION_SEQUENCE_ENV_VAR: " "},
            )
            is None
        )

    @pytest.mark.parametrize("bad", ["support-chat-42", "not a ulid", "01HZ", "x" * 26])
    def test_invalid_cli_session_id_raises(self, bad: str) -> None:
        with pytest.raises(InvalidSessionMembershipError):
            resolve_session_membership(cli_session_id=bad, environ={})

    def test_invalid_sdk_session_id_raises(self) -> None:
        with pytest.raises(InvalidSessionMembershipError):
            resolve_session_membership(sdk_session_id="nope", environ={})

    def test_cli_sequence_without_session_id_raises(self) -> None:
        with pytest.raises(InvalidSessionMembershipError):
            resolve_session_membership(cli_sequence=0, environ={})

    def test_negative_cli_sequence_raises(self) -> None:
        with pytest.raises(InvalidSessionMembershipError):
            resolve_session_membership(cli_session_id=SID, cli_sequence=-1, environ={})

    def test_invalid_env_session_id_warns_and_falls_through_to_sdk(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.session"):
            resolved = resolve_session_membership(
                sdk_session_id=SID,
                environ={SESSION_ID_ENV_VAR: "not-a-ulid"},
            )
        assert resolved is not None
        assert (resolved.session_id, resolved.source) == (SID, "sdk-arg")
        assert any("not-a-ulid" in r.message for r in caplog.records)

    def test_env_sequence_without_session_id_warns_and_falls_through(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.session"):
            resolved = resolve_session_membership(
                sdk_session_id=SID, environ={SESSION_SEQUENCE_ENV_VAR: "4"}
            )
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence, resolved.source) == (
            SID,
            None,
            "sdk-arg",
        )

    def test_invalid_env_sequence_drops_sequence_keeps_session(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="novafabric.capture.session"):
            resolved = resolve_session_membership(
                environ={SESSION_ID_ENV_VAR: SID, SESSION_SEQUENCE_ENV_VAR: "two"}
            )
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence, resolved.source) == (
            SID,
            None,
            "env-var",
        )

    def test_uses_os_environ_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, SID)
        monkeypatch.setenv(SESSION_SEQUENCE_ENV_VAR, "5")
        resolved = resolve_session_membership()
        assert resolved is not None
        assert (resolved.session_id, resolved.sequence, resolved.source) == (
            SID,
            5,
            "env-var",
        )


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
    """The backward-compat golden really is a pre-ADR-0122 manifest."""
    manifest = json.loads((FIXTURE_DIR / "valid-absent.json").read_text())
    assert "session_id" not in manifest
    assert "sequence" not in manifest


# ---------------------------------------------------------------------------
# Orchestrator capture: fields recorded verbatim; absence changes nothing
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
    def test_session_flags_recorded(self, tmp_path: Path) -> None:
        manifest = _capture(tmp_path, session_id=SID, session_sequence=2)
        assert manifest["session_id"] == SID
        assert manifest["sequence"] == 2
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_session_id_alone_recorded_without_sequence(self, tmp_path: Path) -> None:
        manifest = _capture(tmp_path, session_id=SID)
        assert manifest["session_id"] == SID
        assert "sequence" not in manifest
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_env_vars_recorded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, SID)
        monkeypatch.setenv(SESSION_SEQUENCE_ENV_VAR, "0")
        manifest = _capture(tmp_path)
        assert manifest["session_id"] == SID
        assert manifest["sequence"] == 0

    def test_cli_flag_beats_env_var(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(SESSION_ID_ENV_VAR, OTHER_SID)
        manifest = _capture(tmp_path, session_id=SID, session_sequence=1)
        assert manifest["session_id"] == SID
        assert manifest["sequence"] == 1

    def test_absent_by_default_and_still_schema_valid(self, tmp_path: Path) -> None:
        manifest = _capture(tmp_path)
        assert "session_id" not in manifest
        assert "sequence" not in manifest
        _validator(PACKAGED_SCHEMA).validate(manifest)

    def test_invalid_session_id_raises_before_any_capsule_is_written(
        self, tmp_path: Path
    ) -> None:
        orch = CaptureOrchestrator(base_dir=tmp_path / "runs")
        with pytest.raises(InvalidSessionMembershipError):
            orch.run(command=[sys.executable, "-c", "pass"], session_id="not-a-ulid")
        assert list((tmp_path / "runs").iterdir()) == []


# ---------------------------------------------------------------------------
# SDK decorator: sdk-arg tier; env vars take precedence over the arguments
# ---------------------------------------------------------------------------


class TestSdkCapture:
    def test_sdk_args_recorded(self, tmp_path: Path) -> None:
        from novafabric.sdk.agent import agent

        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir, session_id=SID,
               session_sequence=1)
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert manifest["session_id"] == SID
        assert manifest["sequence"] == 1

    def test_env_vars_beat_sdk_args(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from novafabric.sdk.agent import agent

        monkeypatch.setenv(SESSION_ID_ENV_VAR, OTHER_SID)
        monkeypatch.setenv(SESSION_SEQUENCE_ENV_VAR, "7")
        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir, session_id=SID,
               session_sequence=1)
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert manifest["session_id"] == OTHER_SID
        assert manifest["sequence"] == 7

    def test_sdk_default_leaves_fields_absent(self, tmp_path: Path) -> None:
        from novafabric.sdk.agent import agent

        cap_dir = tmp_path / "capsule"

        @agent(name="a", version="1.0", capsule_dir=cap_dir)
        def noop() -> None:
            pass

        noop()
        manifest = yaml.safe_load((cap_dir / "capsule.yaml").read_text())
        assert "session_id" not in manifest
        assert "sequence" not in manifest
