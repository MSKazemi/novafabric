# Copyright 2024 NovaFabric Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for the ADR-0314 capture stage: spans, walkers, failure fallbacks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from novafabric.masking import MaskerSpec, MaskingPipeline
from novafabric.masking._registry import LoadedMasker
from novafabric.masking.examples import EmailMasker
from novafabric.pii.capture_stage import ChainedCaptureStages, SubjectEnvelopeStage
from novafabric.pii.dek import DEKStore
from novafabric.pii.envelope import (
    ERASED,
    TOKEN_RE,
    EnvelopeKeyring,
    read_capsule_file,
    render_value,
)

PEPPER = b"stage-pepper"
S = "patient-0042"
RUN = "run-stage"


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "dek.db"


def _stage(store_path: Path, subject: str = S, **kw: Any) -> SubjectEnvelopeStage:
    return SubjectEnvelopeStage(
        subject, pepper=PEPPER, dek_store_factory=lambda: DEKStore(store_path), **kw
    )


def _keyring(store_path: Path) -> EnvelopeKeyring:
    return EnvelopeKeyring(dek_store=DEKStore(store_path), pepper=PEPPER)


def test_constructor_rejects_empty_subject_and_missing_pepper() -> None:
    with pytest.raises(ValueError):
        SubjectEnvelopeStage("", pepper=PEPPER)
    with pytest.raises(ValueError):
        SubjectEnvelopeStage(S, pepper=b"")


def test_literal_subject_id_is_encrypted_but_not_inside_a_larger_token(
    store_path: Path,
) -> None:
    stage = _stage(store_path)
    out, _, errors = stage.mask_mapping(
        {"a": f"record for {S}.", "b": f"x{S}y", "c": "nothing here"}, "capsule.yaml",
        "capsule-yaml", RUN,
    )
    assert errors == []
    assert S not in out["a"] and TOKEN_RE.search(out["a"])
    assert out["b"] == f"x{S}y"  # glued to letters: part of another identifier
    assert out["c"] == "nothing here"
    kr = _keyring(store_path)
    assert render_value(out["a"], keyring=kr, run_id=RUN, field_path="capsule.yaml a") == (
        f"record for {S}."
    )


def test_overlapping_spans_merge_into_one_envelope(store_path: Path) -> None:
    stage = _stage(store_path, subject="John Smith")
    out, _, _ = stage.mask_mapping({"v": "Hi John Smith!"}, "capsule.yaml", "k", RUN)
    tokens = TOKEN_RE.findall(out["v"])
    assert len(tokens) == 1
    assert "PERSON" in out["v"] and "SUBJECT_ID" in out["v"]


def test_existing_markers_are_never_reprocessed(store_path: Path) -> None:
    stage = _stage(store_path)
    first, _, _ = stage.mask_mapping({"v": f"id {S}"}, "capsule.yaml", "k", RUN)
    again, _, _ = stage.mask_mapping(first, "capsule.yaml", "k", RUN)
    assert again == first
    masked = {"v": "[REDACTED:sha256:abc:EMAIL] and [MASKED:email]"}
    assert stage.mask_mapping(masked, "capsule.yaml", "k", RUN)[0] == masked


def test_all_file_walkers_round_trip(tmp_path: Path, store_path: Path) -> None:
    cap = tmp_path / "cap"
    (cap / "outputs").mkdir(parents=True)
    (cap / "model-calls.jsonl").write_text(
        json.dumps({"messages": [{"content": f"I am {S}"}]}) + "\n\nnot json {S}\n".replace(
            "{S}", S
        )
    )
    (cap / "env.lock").write_text(yaml.dump({"note": f"owner {S}"}))
    (cap / "outputs" / "stdout.txt").write_text(f"hello {S}\nbye\n")
    (cap / "outputs" / "blob.bin").write_bytes(b"\x00\x01" + S.encode())

    stage = _stage(store_path)
    findings, errors = stage.run(cap, RUN)
    assert findings == [] and errors == []
    for rel in ("model-calls.jsonl", "env.lock", "outputs/stdout.txt"):
        assert S.encode() not in (cap / rel).read_bytes(), rel
    # Binary artifacts are not offered to the stage (documented limitation).
    assert S.encode() in (cap / "outputs" / "blob.bin").read_bytes()

    kr = _keyring(store_path)
    calls = read_capsule_file(cap, "model-calls.jsonl", keyring=kr, run_id=RUN)
    assert calls[0]["messages"][0]["content"] == f"I am {S}"
    assert calls[1] == f"not json {S}"
    assert read_capsule_file(cap, "env.lock", keyring=kr, run_id=RUN) == {"note": f"owner {S}"}
    assert read_capsule_file(cap, "outputs/stdout.txt", keyring=kr, run_id=RUN) == (
        f"hello {S}\nbye\n"
    )
    manifest = json.loads((cap / "redaction_manifest.json").read_text())
    assert len(manifest["entries"]) == 4

    kr.dek_store.erase_subject(S, capsule_ids=[], retention_months=0)  # type: ignore[union-attr]
    kr.forget()
    assert read_capsule_file(cap, "outputs/stdout.txt", keyring=kr, run_id=RUN) == (
        f"hello {ERASED}\nbye\n"
    )
    kr.close()


def test_unexpected_file_failure_retries_mask_only_then_drops(
    tmp_path: Path, store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cap = tmp_path / "cap"
    (cap / "outputs").mkdir(parents=True)
    (cap / "outputs" / "stdout.txt").write_text(f"hi {S}\n")
    stage = _stage(store_path)
    real = SubjectEnvelopeStage._text
    calls = {"n": 0}

    def flaky(self: SubjectEnvelopeStage, *a: Any, **k: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        real(self, *a, **k)

    monkeypatch.setattr(SubjectEnvelopeStage, "_text", flaky)
    _, errors = stage.run(cap, RUN, targets=[("outputs/stdout.txt", "output-artifact")])
    text = (cap / "outputs" / "stdout.txt").read_text()
    assert S not in text and "[REDACTED:sha256:" in text
    assert errors[-1]["action_taken"] == "redact"

    (cap / "outputs" / "stdout.txt").write_text(f"hi {S}\n")

    def always(self: SubjectEnvelopeStage, *a: Any, **k: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(SubjectEnvelopeStage, "_text", always)
    _, errors = stage.run(cap, RUN, targets=[("outputs/stdout.txt", "output-artifact")])
    assert (cap / "outputs" / "stdout.txt").read_bytes() == b""
    assert errors[-1]["action_taken"] == "drop"


def test_manifest_walk_failure_falls_back_to_masking(
    store_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = _stage(store_path)
    real = SubjectEnvelopeStage._get_dek

    def boom(self: SubjectEnvelopeStage) -> Any:
        raise RuntimeError("unexpected")

    monkeypatch.setattr(SubjectEnvelopeStage, "_get_dek", boom)
    out, _, errors = stage.mask_mapping({"v": f"id {S}"}, "capsule.yaml", "k", RUN)
    assert S not in out["v"] and "[REDACTED:sha256:" in out["v"]
    assert errors and errors[-1]["action_taken"] == "redact"
    monkeypatch.setattr(SubjectEnvelopeStage, "_get_dek", real)


def test_chained_stages_run_operator_maskers_first(tmp_path: Path, store_path: Path) -> None:
    pipeline = MaskingPipeline(
        [LoadedMasker(masker=EmailMasker(), spec=MaskerSpec(id="novafabric-email"))]
    )
    chain = ChainedCaptureStages([pipeline, _stage(store_path), None])
    out, findings, errors = chain.mask_mapping(
        {"v": f"a@example.com and {S}"}, "capsule.yaml", "capsule-yaml", RUN
    )
    assert "[MASKED:email]" in out["v"] and TOKEN_RE.search(out["v"]) and S not in out["v"]
    assert any(f["masker_id"] == "novafabric-email" for f in findings)
    assert errors == []

    cap = tmp_path / "cap"
    (cap / "outputs").mkdir(parents=True)
    (cap / "outputs" / "stdout.txt").write_text(f"{S}\n")
    _, run_errors = chain.run(cap, RUN)
    assert run_errors == [] and S not in (cap / "outputs" / "stdout.txt").read_text()


def test_keyring_from_home_without_a_store_reports_erased(tmp_path: Path, store_path: Path) -> None:
    stage = _stage(store_path)
    out, _, _ = stage.mask_mapping({"v": S}, "capsule.yaml", "k", RUN)
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    kr = EnvelopeKeyring.from_home(empty_home, PEPPER)
    assert render_value(out["v"], keyring=kr, run_id=RUN, field_path="capsule.yaml v") == ERASED
    assert not (empty_home / "dek.db").exists()
    kr.close()


# --- review compliance-privacy-17: two common cases left the PII in plaintext ---------


def _bare_capsule(tmp_path: Path) -> Path:
    cap = tmp_path / "cap"
    (cap / "outputs").mkdir(parents=True)
    return cap


def test_an_email_glued_to_cjk_text_is_encrypted(tmp_path: Path, store_path: Path) -> None:
    """CJK has no spaces; the "glued to a larger token" rule used str.isalnum(), which
    is true for CJK letters, so the address next to them stayed plaintext."""
    cap = _bare_capsule(tmp_path)
    (cap / "outputs" / "stdout.txt").write_text("連絡先alice@example.com です\n", encoding="utf-8")
    _stage(store_path).run(cap, RUN)
    text = (cap / "outputs" / "stdout.txt").read_text(encoding="utf-8")
    assert "alice@example.com" not in text
    assert TOKEN_RE.search(text)
    assert text.startswith("連絡先")


def test_an_ascii_glued_match_is_still_left_alone(tmp_path: Path, store_path: Path) -> None:
    """The rule's purpose: a match inside an ASCII token (an id, a digest) is not PII."""
    cap = _bare_capsule(tmp_path)
    line = f"ref=x{S}y\n"
    (cap / "outputs" / "stdout.txt").write_text(line)
    _stage(store_path).run(cap, RUN)
    assert (cap / "outputs" / "stdout.txt").read_text() == line


def test_a_latin1_text_file_is_protected_not_skipped_as_binary(
    tmp_path: Path, store_path: Path
) -> None:
    """Not valid UTF-8 read as binary and was skipped, with the subject in plaintext."""
    cap = _bare_capsule(tmp_path)
    raw = f"Müller {S} écrit\n".encode("latin-1")
    (cap / "outputs" / "notes.txt").write_bytes(raw)
    _stage(store_path).run(cap, RUN)
    after = (cap / "outputs" / "notes.txt").read_bytes()
    assert S.encode() not in after
    assert after.startswith("Müller ".encode("latin-1"))  # other bytes kept as they were
    assert after.endswith(" écrit\n".encode("latin-1"))
    kr = _keyring(store_path)
    assert read_capsule_file(cap, "outputs/notes.txt", keyring=kr, run_id=RUN) == raw.decode("latin-1")
