"""Emitter: opt-in config, hygiene, signing, fail-safe emission (ADR-0137 D4/D5/D6)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from novafabric.events.emitter import (
    EventsConfig,
    LifecycleEventEmitter,
    build_emitter_from_env,
    build_sink,
    emit_lifecycle_event,
    load_config_from_env,
)
from novafabric.events.model import EventType, LifecycleEvent, Subject, SubjectKind
from novafabric.events.signing import canonical_body, sign_record, verify_record
from novafabric.events.sinks import FileSink, MultiSink, NullSink, WebhookSink


def _event() -> LifecycleEvent:
    return LifecycleEvent(
        type=EventType.CAPSULE_CREATED,
        subject=Subject(kind=SubjectKind.CAPSULE, ref="run-abc"),
        payload={"status": "success"},
    )


class TestOptIn:
    def test_unconfigured_env_yields_null_sink(self) -> None:
        emitter = build_emitter_from_env()
        assert emitter.enabled is False

    def test_unconfigured_emit_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        emit_lifecycle_event(
            event_type="capsule.created",
            subject_kind="capsule",
            subject_ref="run-abc",
        )
        assert list(tmp_path.iterdir()) == []  # nothing fires unconfigured

    def test_sink_selection(self, tmp_path: Path) -> None:
        assert isinstance(build_sink(EventsConfig()), NullSink)
        assert isinstance(
            build_sink(EventsConfig(log_path=tmp_path / "e.jsonl")), FileSink
        )
        assert isinstance(
            build_sink(EventsConfig(webhook_urls=("http://127.0.0.1:1/h",))),
            WebhookSink,
        )
        both = build_sink(
            EventsConfig(
                log_path=tmp_path / "e.jsonl",
                webhook_urls=("http://127.0.0.1:1/h", "http://127.0.0.1:2/h"),
            )
        )
        assert isinstance(both, MultiSink)


class TestConfigFromEnv:
    def test_defaults(self) -> None:
        config = load_config_from_env({})
        assert config.enabled is False
        assert config.max_retries == 2
        assert config.timeout_s == 5.0
        assert config.sign_secret is None

    def test_full_config(self, tmp_path: Path) -> None:
        config = load_config_from_env({
            "NOVA_EVENTS_LOG": str(tmp_path / "events.jsonl"),
            "NOVA_EVENTS_WEBHOOK": "http://a.internal/h, http://b.internal/h",
            "NOVA_EVENTS_MAX_RETRIES": "5",
            "NOVA_EVENTS_TIMEOUT_S": "1.5",
            "NOVA_EVENTS_SIGN_SECRET": "s3cret",
            "NOVA_EVENTS_SIGN_KEYID": "ci-shared-2026",
        })
        assert config.enabled is True
        assert config.log_path == tmp_path / "events.jsonl"
        assert config.webhook_urls == ("http://a.internal/h", "http://b.internal/h")
        assert config.max_retries == 5
        assert config.timeout_s == 1.5
        assert config.sign_secret == b"s3cret"
        assert config.sign_keyid == "ci-shared-2026"

    def test_invalid_numbers_fall_back(self) -> None:
        config = load_config_from_env({
            "NOVA_EVENTS_MAX_RETRIES": "many",
            "NOVA_EVENTS_TIMEOUT_S": "soon",
        })
        assert config.max_retries == 2
        assert config.timeout_s == 5.0

    def test_keyid_without_secret_emits_unsigned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed on signing only: unsigned emission, transition unblocked."""
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        monkeypatch.setenv("NOVA_EVENTS_SIGN_KEYID", "ci")
        build_emitter_from_env().emit(_event())
        record = json.loads(log.read_text().splitlines()[0])
        assert "signature" not in record


class TestEmission:
    def test_emit_appends_to_log(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        emit_lifecycle_event(
            event_type="capsule.created",
            subject_kind="capsule",
            subject_ref="run-abc",
            subject_digest="sha256:" + "ab" * 32,
            payload={"status": "success", "model_call_count": 3},
            source="nova capture",
        )
        record = json.loads(log.read_text().splitlines()[0])
        assert record["type"] == "capsule.created"
        assert record["subject"] == {
            "kind": "capsule", "ref": "run-abc", "digest": "sha256:" + "ab" * 32,
        }
        assert record["payload"]["model_call_count"] == 3
        assert record["source"] == "nova capture"

    def test_emit_fires_webhook(
        self, webhook_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_EVENTS_WEBHOOK", webhook_server.url)
        emit_lifecycle_event(
            event_type="policy.failed",
            subject_kind="policy",
            subject_ref="promotion:agent-a@1.4.0",
            payload={"decision": "deny"},
        )
        assert len(webhook_server.received) == 1
        record = json.loads(webhook_server.received[0]["body"])
        assert record["type"] == "policy.failed"

    def test_secret_masked_before_any_sink(
        self, tmp_path: Path, webhook_server: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        monkeypatch.setenv("NOVA_EVENTS_WEBHOOK", webhook_server.url)
        emit_lifecycle_event(
            event_type="capsule.created",
            subject_kind="capsule",
            subject_ref="run-abc",
            payload={"note": "oops sk-ant-" + "a1B2" * 8},
        )
        assert "sk-ant-" not in log.read_text()
        assert b"sk-ant-" not in webhook_server.received[0]["body"]

    def test_signed_emission_is_verifiable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = tmp_path / "events.jsonl"
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(log))
        monkeypatch.setenv("NOVA_EVENTS_SIGN_SECRET", "s3cret")
        monkeypatch.setenv("NOVA_EVENTS_SIGN_KEYID", "ci-shared-2026")
        emit_lifecycle_event(
            event_type="capsule.created",
            subject_kind="capsule",
            subject_ref="run-abc",
        )
        record = json.loads(log.read_text().splitlines()[0])
        assert record["signature"]["alg"] == "hmac-sha256"
        assert record["signature"]["keyid"] == "ci-shared-2026"
        assert verify_record(record, b"s3cret") is True
        assert verify_record(record, b"wrong") is False
        # secret never appears in the emitted record
        assert "s3cret" not in json.dumps(record)


class TestFailSafe:
    def test_unwritable_log_path_never_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a directory")
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(blocker / "events.jsonl"))
        emit_lifecycle_event(
            event_type="capsule.created",
            subject_kind="capsule",
            subject_ref="run-abc",
        )  # must not raise

    def test_invalid_arguments_never_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NOVA_EVENTS_LOG", str(tmp_path / "events.jsonl"))
        emit_lifecycle_event(
            event_type="not.a.type",
            subject_kind="capsule",
            subject_ref="run-abc",
        )  # must not raise

    def test_emitter_swallows_sink_exception(self) -> None:
        class BoomSink:
            def send(self, record: dict[str, Any]) -> None:
                raise RuntimeError("boom")

        LifecycleEventEmitter(BoomSink()).emit(_event())  # must not raise


class TestSigningHelpers:
    def test_canonical_body_excludes_signature(self) -> None:
        record = _event().to_record()
        signed = sign_record(record, b"k", "id")
        assert canonical_body(signed) == canonical_body(record)

    def test_verify_rejects_unsigned(self) -> None:
        assert verify_record(_event().to_record(), b"k") is False
