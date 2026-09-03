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

"""NF-161/163 — C2PA hard binding to a captured media blob (ADR-0148 D1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from novafabric.trust.provenance.c2pa_bind import (
    NO_CERT_VERIFIER,
    SIDECAR_SUFFIX,
    attach_facet,
    bind_manifest,
    build_facet,
    discover_sidecar_manifests,
    facet_from_capsule,
    manifest_digest,
    normalise_content_hash,
    output_entries,
    parse_manifest,
    sidecar_path_for,
    unverified_bindings,
)
from trust._provenance_fixtures import (
    IMAGE_BYTES,
    IMAGE_HASH,
    IMAGE_HEX,
    OTHER_HASH,
    a_capsule,
    a_manifest,
)

# --- normalisation -----------------------------------------------------------


def test_normalise_accepts_prefixed_and_bare_hex() -> None:
    assert normalise_content_hash(IMAGE_HASH) == IMAGE_HASH
    assert normalise_content_hash(IMAGE_HEX) == IMAGE_HASH
    assert normalise_content_hash(IMAGE_HEX.upper()) == IMAGE_HASH


def test_normalise_rejects_non_sha256() -> None:
    for bad in [None, 42, "", "sha512:" + "a" * 128, "sha256:nothex", "deadbeef"]:
        assert normalise_content_hash(bad) is None


def test_manifest_digest_is_serialisation_independent() -> None:
    """The digest identifies the manifest, not one file's key order."""
    a = {"active_manifest": "m", "manifests": {"m": {"assertions": []}}}
    b = {"manifests": {"m": {"assertions": []}}, "active_manifest": "m"}
    assert manifest_digest(a) == manifest_digest(b)
    c = {"active_manifest": "n", "manifests": {"n": {"assertions": []}}}
    assert manifest_digest(a) != manifest_digest(c)


# --- parsing -----------------------------------------------------------------


def test_parse_reads_claimed_hash_kind_and_signer() -> None:
    parsed = parse_manifest(a_manifest(signed=True))
    assert parsed is not None
    assert parsed.kind == "c2pa"
    assert parsed.active_manifest_ok is True
    assert parsed.claimed_hash == IMAGE_HASH
    assert parsed.signer is not None
    assert parsed.signer.subject == "CN=Camera Co"


def test_parse_reads_content_credentials_kind() -> None:
    parsed = parse_manifest(a_manifest(kind="content_credentials"))
    assert parsed is not None
    assert parsed.kind == "content_credentials"


def test_parse_returns_none_for_non_manifest() -> None:
    for bad in [None, 7, "text", [], {"no_manifests": True}]:
        assert parse_manifest(bad) is None


def test_dangling_active_manifest_parses_as_not_ok_rather_than_none() -> None:
    """'We read it and its active manifest is dangling' is a finding, not a non-manifest."""
    parsed = parse_manifest(a_manifest(resolvable=False))
    assert parsed is not None
    assert parsed.active_manifest_ok is False
    assert parsed.claimed_hash is None


def test_parse_tolerates_malformed_assertions() -> None:
    doc = {
        "active_manifest": "m",
        "manifests": {"m": {"assertions": ["not-a-dict", {"label": "x"}, 3]}},
    }
    parsed = parse_manifest(doc)
    assert parsed is not None
    assert parsed.claimed_hash is None


# --- binding -----------------------------------------------------------------


def test_bind_against_blob_bytes_is_marked_as_such() -> None:
    """AC1 — the strong basis: the claim is checked by re-hashing the bytes we hold."""
    entry = bind_manifest(
        content_hash=IMAGE_HASH, document=a_manifest(), blob_bytes=IMAGE_BYTES
    )
    assert entry is not None
    assert entry.bound_against == "blob_bytes"
    assert entry.verified.hard_binding_ok is True
    assert entry.verified.active_manifest_ok is True


def test_bind_without_blob_uses_recorded_hash_and_says_so() -> None:
    """The weaker basis is legitimate but must never serialise as the stronger one."""
    entry = bind_manifest(content_hash=IMAGE_HASH, document=a_manifest())
    assert entry is not None
    assert entry.bound_against == "recorded_hash"
    assert entry.verified.hard_binding_ok is True


def test_recorded_hash_binding_cannot_detect_a_tampered_blob() -> None:
    """Why the two bases are distinguished: the same manifest, the same recorded hash,
    opposite verdicts depending on whether the bytes were actually re-hashed."""
    tampered = b"these are not the bytes the hash was taken over"
    without_bytes = bind_manifest(content_hash=IMAGE_HASH, document=a_manifest())
    with_bytes = bind_manifest(
        content_hash=IMAGE_HASH, document=a_manifest(), blob_bytes=tampered
    )
    assert without_bytes is not None and with_bytes is not None
    assert without_bytes.verified.hard_binding_ok is True
    assert with_bytes.verified.hard_binding_ok is False
    assert without_bytes.bound_against != with_bytes.bound_against


def test_hash_mismatch_is_recorded_as_a_failed_binding_not_an_error() -> None:
    """AC2 — a failed binding is evidence; the entry is still written."""
    entry = bind_manifest(
        content_hash=IMAGE_HASH,
        document=a_manifest(claimed_hash=OTHER_HASH),
        blob_bytes=IMAGE_BYTES,
    )
    assert entry is not None
    assert entry.verified.hard_binding_ok is False
    assert entry.bound_content_hash == IMAGE_HASH


def test_manifest_with_no_hard_binding_yields_none_not_false() -> None:
    """A missing claim and a broken claim are different facts."""
    entry = bind_manifest(
        content_hash=IMAGE_HASH, document=a_manifest(claimed_hash=None)
    )
    assert entry is not None
    assert entry.verified.hard_binding_ok is None


def test_cert_chain_ok_is_none_even_for_a_signed_looking_manifest() -> None:
    """AC3 — never inferred from a signature being present (the NF-171 lesson)."""
    signed = bind_manifest(
        content_hash=IMAGE_HASH, document=a_manifest(signed=True), blob_bytes=IMAGE_BYTES
    )
    unsigned = bind_manifest(
        content_hash=IMAGE_HASH, document=a_manifest(signed=False), blob_bytes=IMAGE_BYTES
    )
    assert signed is not None and unsigned is not None
    assert signed.signer is not None, "the signer identity IS read"
    assert signed.verified.cert_chain_ok is None, "but the chain is NOT verified"
    assert unsigned.verified.cert_chain_ok is None
    assert signed.verified.cert_chain_reason == NO_CERT_VERIFIER


def test_bind_rejects_an_unusable_content_hash() -> None:
    assert bind_manifest(content_hash="not-a-hash", document=a_manifest()) is None


def test_bind_returns_none_for_a_non_manifest() -> None:
    assert bind_manifest(content_hash=IMAGE_HASH, document={"nope": 1}) is None


def test_manifest_bytes_not_recorded_as_captured_by_default() -> None:
    """AC8 / I-2 — reference-metadata-by-default."""
    default = bind_manifest(content_hash=IMAGE_HASH, document=a_manifest())
    opted_in = bind_manifest(
        content_hash=IMAGE_HASH, document=a_manifest(), capture_manifest_bytes=True
    )
    assert default is not None and opted_in is not None
    assert default.manifest_bytes_captured is False
    assert opted_in.manifest_bytes_captured is True


# --- NF-163 output direction -------------------------------------------------


def test_output_direction_carries_producer_and_nf094_crosslink() -> None:
    """AC6 — NF-163's per-artifact receipt names its producer and cross-links NF-094."""
    receipt_digest = f"sha256:{'ee' * 32}"
    entry = bind_manifest(
        content_hash=IMAGE_HASH,
        document=a_manifest(kind="content_credentials"),
        direction="output",
        blob_bytes=IMAGE_BYTES,
        producing_model="img-gen-v3",
        producing_run_id="run_1735",
        art50_marking_claimed=True,
        nf094_receipt_digest=receipt_digest,
    )
    assert entry is not None
    assert entry.direction == "output"
    assert entry.producing_model == "img-gen-v3"
    assert entry.producing_run_id == "run_1735"
    assert entry.art50_marking_claimed is True
    assert entry.nf094_receipt_digest == receipt_digest


def test_input_direction_never_carries_producer_fields() -> None:
    """An input entry cannot claim a producing model even if one is passed in."""
    entry = bind_manifest(
        content_hash=IMAGE_HASH,
        document=a_manifest(),
        direction="input",
        producing_model="img-gen-v3",
        art50_marking_claimed=True,
        nf094_receipt_digest=f"sha256:{'ee' * 32}",
    )
    assert entry is not None
    assert entry.producing_model is None
    assert entry.art50_marking_claimed is None
    assert entry.nf094_receipt_digest is None


# --- discovery + facet -------------------------------------------------------


def test_sidecar_discovery_finds_and_keys_by_content_hash(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    found = discover_sidecar_manifests(capsule)
    assert set(found) == {IMAGE_HASH}


def test_sidecar_discovery_skips_unparseable_and_misnamed(tmp_path: Path) -> None:
    """A broken sidecar must not stop the readable ones from binding."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    (capsule / "outputs" / f"{'ab' * 32}{SIDECAR_SUFFIX}").write_text("{not json")
    (capsule / "outputs" / f"nothex{SIDECAR_SUFFIX}").write_text("{}")
    found = discover_sidecar_manifests(capsule)
    assert set(found) == {IMAGE_HASH}


def test_discovery_on_a_capsule_without_outputs_is_empty(tmp_path: Path) -> None:
    capsule = tmp_path / "bare"
    capsule.mkdir()
    assert discover_sidecar_manifests(capsule) == {}


def test_build_facet_binds_a_sidecar_manifest_and_counts_what_it_scanned(
    tmp_path: Path,
) -> None:
    """AC1 end-to-end, and the counts that keep an empty answer legible."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    facet = build_facet(capsule)
    assert facet is not None
    assert len(facet.entries) == 1
    assert facet.entries[0].bound_against == "blob_bytes"
    assert facet.entries[0].verified.hard_binding_ok is True
    assert facet.media_parts_scanned == 1
    assert facet.manifests_found == 1


def test_build_facet_marks_declared_output_hashes_as_output(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    facet = build_facet(
        capsule,
        output_hashes=[IMAGE_HASH],
        producing_model="img-gen-v3",
        producing_run_id="run_1",
        art50_marking_claimed=True,
    )
    assert facet is not None
    outputs = output_entries(facet)
    assert len(outputs) == 1
    assert outputs[0].producing_model == "img-gen-v3"


def test_build_facet_returns_none_when_nothing_binds(tmp_path: Path) -> None:
    """AC7 / I-3 — fail-open. An empty facet would read as 'we checked, there is none'."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=None)
    assert build_facet(capsule) is None


def test_build_facet_on_a_capsule_with_no_media_is_none(tmp_path: Path) -> None:
    capsule = tmp_path / "empty"
    capsule.mkdir()
    assert build_facet(capsule) is None


def test_build_facet_ignores_a_manifest_for_media_the_capsule_lacks(
    tmp_path: Path,
) -> None:
    """A manifest is bound to the media it is named for, never to whatever is present."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    facet = build_facet(capsule, manifests={OTHER_HASH: a_manifest()})
    assert facet is None


def test_build_facet_binds_without_blob_bytes_present(tmp_path: Path) -> None:
    """Reference-metadata-only capsules still bind — on the weaker basis, marked."""
    capsule = a_capsule(tmp_path, blob=None, sidecar=a_manifest())
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "recorded_hash"


def test_build_facet_deduplicates_repeated_media_parts(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    lines = (capsule / "model-calls.jsonl").read_text()
    (capsule / "model-calls.jsonl").write_text(lines + lines)
    facet = build_facet(capsule)
    assert facet is not None
    assert len(facet.entries) == 1
    assert facet.media_parts_scanned == 2, "both parts were scanned, one entry written"


# --- facet round-trip --------------------------------------------------------


def test_attach_and_read_back_round_trips(tmp_path: Path) -> None:
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    facet = build_facet(capsule)
    assert facet is not None
    manifest: dict[str, Any] = {"run_id": "run_1"}
    attach_facet(manifest, facet)
    assert "media_provenance" in manifest["facets"]
    read_back = facet_from_capsule(manifest)
    assert read_back is not None
    assert read_back.entries[0].bound_content_hash == IMAGE_HASH
    assert read_back.entries[0].verified.cert_chain_ok is None


def test_facet_from_capsule_is_none_for_capsules_without_one() -> None:
    assert facet_from_capsule({}) is None
    assert facet_from_capsule({"facets": "not-a-mapping"}) is None
    assert facet_from_capsule({"facets": {}}) is None
    assert facet_from_capsule({"facets": {"media_provenance": {"entries": "bad"}}}) is None


def test_unverified_bindings_reports_failed_and_unclaimed_separably() -> None:
    """Both mean 'not established', but the caller can still tell them apart."""
    failed = bind_manifest(
        content_hash=IMAGE_HASH,
        document=a_manifest(claimed_hash=OTHER_HASH),
        blob_bytes=IMAGE_BYTES,
    )
    unclaimed = bind_manifest(
        content_hash=OTHER_HASH, document=a_manifest(claimed_hash=None)
    )
    ok = bind_manifest(
        content_hash=IMAGE_HASH, document=a_manifest(), blob_bytes=IMAGE_BYTES
    )
    assert failed is not None and unclaimed is not None and ok is not None
    from novafabric.trust.provenance.c2pa_bind import MediaProvenanceFacet

    facet = MediaProvenanceFacet(entries=[failed, unclaimed, ok])
    unestablished = unverified_bindings(facet)
    assert len(unestablished) == 2
    assert [e.verified.hard_binding_ok for e in unestablished] == [False, None]


def test_agent_produced_media_on_the_response_path_binds_as_output(
    tmp_path: Path,
) -> None:
    """NF-163's realistic path: the image the agent produced arrives on the response,
    not the request. Binding must reach it there — a facet that only ever saw request
    media would report zero output receipts for every generating run."""
    capsule = a_capsule(
        tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest(kind="content_credentials"),
        on_response=True,
    )
    facet = build_facet(
        capsule,
        output_hashes=[IMAGE_HASH],
        producing_model="img-gen-v3",
        producing_run_id="run_1",
        art50_marking_claimed=True,
        nf094_receipt_digest=f"sha256:{'ee' * 32}",
    )
    assert facet is not None
    receipts = output_entries(facet)
    assert len(receipts) == 1
    assert receipts[0].manifest_kind == "content_credentials"
    assert receipts[0].verified.hard_binding_ok is True
    assert receipts[0].nf094_receipt_digest is not None


def test_a_sidecar_holding_valid_json_that_is_not_a_manifest_is_skipped(
    tmp_path: Path,
) -> None:
    """Parseable JSON is not a manifest. The file is read, rejected, and produces no
    entry — never an entry asserting a binding nothing established."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES)
    sidecar_path_for(capsule, IMAGE_HASH).write_text(json.dumps({"hello": "world"}))
    assert build_facet(capsule) is None


def test_a_blob_ref_that_does_not_resolve_falls_back_to_the_recorded_hash(
    tmp_path: Path,
) -> None:
    """A capsule can name a blob it no longer holds (pruned, or moved). Binding must
    degrade to the weaker basis and SAY so, not silently claim byte-level evidence."""
    capsule = a_capsule(tmp_path, blob=IMAGE_BYTES, sidecar=a_manifest())
    blob = capsule / "outputs" / f"{IMAGE_HEX}.png"
    assert blob.exists()
    blob.unlink()
    facet = build_facet(capsule)
    assert facet is not None
    assert facet.entries[0].bound_against == "recorded_hash"
