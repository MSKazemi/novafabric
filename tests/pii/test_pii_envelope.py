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
"""Unit tests for the per-subject PII envelope (ADR-0314): AEAD, AAD binding, tamper."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from novafabric.capture.secrets import redact_secrets_in_text
from novafabric.pii.dek import DEKStore
from novafabric.pii.envelope import (
    ERASED,
    LOCKED,
    TAMPERED,
    TOKEN_RE,
    EnvelopeFormatError,
    EnvelopeIntegrityError,
    EnvelopeKeyring,
    PIIEnvelope,
    SubjectKeyErasedError,
    open_value,
    render_value,
    seal_value,
    subject_ref,
)

REPO = Path(__file__).resolve().parents[2]
PEPPER = b"unit-test-pepper"
SUBJECT = "alice.smith@example.com"
RUN = "run-0001"
FIELD = "model-calls.jsonl#L1 request.messages[0].content"


@pytest.fixture
def store(tmp_path: Path) -> DEKStore:
    s = DEKStore(tmp_path / "dek.db")
    yield s
    s.close()


def _seal(store: DEKStore, plaintext: str = SUBJECT, *, span_index: int = 0,
          field_path: str = FIELD, run_id: str = RUN, entity: str = "EMAIL") -> PIIEnvelope:
    dek = store.get_or_create_dek(SUBJECT)
    return seal_value(
        plaintext, dek=dek, subject_ref_value=subject_ref(PEPPER, SUBJECT), run_id=run_id,
        field_path=field_path, span_index=span_index, entity_type=entity,
    )


def test_round_trip_and_token_codec(store: DEKStore) -> None:
    env = _seal(store)
    token = env.to_token()
    assert SUBJECT not in token
    assert PIIEnvelope.from_token(token) == env
    dek = store.get_dek(SUBJECT)
    assert dek is not None
    assert open_value(env, dek=dek, run_id=RUN, field_path=FIELD, span_index=0) == SUBJECT


def test_nonce_is_fresh_per_envelope(store: DEKStore) -> None:
    a, b = _seal(store), _seal(store)
    assert a.nonce != b.nonce and a.ciphertext != b.ciphertext


def test_envelope_matches_its_json_schema(store: DEKStore) -> None:
    schema = json.loads((REPO / "schemas" / "pii-envelope.schema.json").read_text())
    jsonschema.validate(_seal(store).model_dump(), schema)


# -- (e) tampering with the ciphertext or the associated data is detected ----


def test_flipped_ciphertext_bit_is_detected(store: DEKStore) -> None:
    env = _seal(store)
    flipped = ("0" if env.ciphertext[0] != "0" else "1") + env.ciphertext[1:]
    bad = env.model_copy(update={"ciphertext": flipped})
    dek = store.get_dek(SUBJECT)
    assert dek is not None
    with pytest.raises(EnvelopeIntegrityError):
        open_value(bad, dek=dek, run_id=RUN, field_path=FIELD, span_index=0)


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": "run-other"},
        {"field_path": "model-calls.jsonl#L2 request.messages[0].content"},
        {"span_index": 1},
    ],
)
def test_changed_associated_data_is_detected(store: DEKStore, change: dict) -> None:
    env = _seal(store)
    dek = store.get_dek(SUBJECT)
    assert dek is not None
    kwargs = {"run_id": RUN, "field_path": FIELD, "span_index": 0, **change}
    with pytest.raises(EnvelopeIntegrityError):
        open_value(env, dek=dek, **kwargs)


def test_relabelled_entity_is_detected(store: DEKStore) -> None:
    env = _seal(store).model_copy(update={"entity_type": "PHONE"})
    dek = store.get_dek(SUBJECT)
    assert dek is not None
    with pytest.raises(EnvelopeIntegrityError):
        open_value(env, dek=dek, run_id=RUN, field_path=FIELD, span_index=0)


def test_reader_renders_tampered_and_swapped_tokens(store: DEKStore) -> None:
    a = _seal(store, "first@example.com", span_index=0).to_token()
    b = _seal(store, "second@example.com", span_index=1).to_token()
    keyring = EnvelopeKeyring(dek_store=store, pepper=PEPPER)
    ok = render_value(f"{a} and {b}", keyring=keyring, run_id=RUN, field_path=FIELD)
    assert ok == "first@example.com and second@example.com"
    swapped = render_value(f"{b} and {a}", keyring=keyring, run_id=RUN, field_path=FIELD)
    assert swapped == f"{TAMPERED} and {TAMPERED}"
    moved = render_value(a, keyring=keyring, run_id=RUN, field_path=FIELD + "x")
    assert moved == TAMPERED


def test_reader_without_key_access_never_reveals(store: DEKStore) -> None:
    token = _seal(store).to_token()
    assert render_value(token, keyring=None, run_id=RUN, field_path=FIELD) == LOCKED
    no_pepper = EnvelopeKeyring(dek_store=store, pepper=None)
    assert render_value(token, keyring=no_pepper, run_id=RUN, field_path=FIELD) == LOCKED


def test_erased_subject_raises_named_error_and_renders_erased(store: DEKStore) -> None:
    token = _seal(store).to_token()
    store.erase_subject(SUBJECT, capsule_ids=[], retention_months=0)
    keyring = EnvelopeKeyring(dek_store=store, pepper=PEPPER)
    with pytest.raises(SubjectKeyErasedError):
        keyring.dek_for(subject_ref(PEPPER, SUBJECT))
    assert render_value(token, keyring=keyring, run_id=RUN, field_path=FIELD) == ERASED


def test_malformed_token_is_a_format_error() -> None:
    with pytest.raises(EnvelopeFormatError):
        PIIEnvelope.from_token("[NFPII:v1:EMAIL:sha256:zz:00:00]")


@pytest.mark.parametrize("length", range(0, 90))
def test_secret_scanner_never_rewrites_an_envelope(store: DEKStore, length: int) -> None:
    """The ADR-0009 residual pass runs after this stage; it must not eat ciphertext.

    Bare 32/40-char alphanumeric and 64-char hex runs are API-key rules; the
    16-char grouping must keep every ciphertext length clear of them.
    """
    token = _seal(store, "x" * length).to_token()
    assert redact_secrets_in_text(token) == token
    assert TOKEN_RE.fullmatch(token)
