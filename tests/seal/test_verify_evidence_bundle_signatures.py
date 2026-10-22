"""``nova verify <bundle>.zip`` checks signatures, ``manifest_hash`` and subjects.

Before this, the verifier recomputed artifact digests only (recipe step 2). Editing a
signed attestation and writing the new file's digest back into ``manifest.json``
produced "Evidence Bundle verification PASSED" — even with the signed subject zeroed
and the DSSE signature broken. These tests pin recipe steps 1, 3 and 4, plus signer
pinning, against one real bundle built with ``nova export-evidence``.

The attacker modelled throughout is the strongest one the format admits: the signing
key ships *inside* the bundle, so every tampering case repairs the artifact digests
and ``manifest_hash`` and may re-sign with a key of its own. Three layers:

* a mutation matrix over :func:`verify_bundle_signatures` in memory — each tamper
  must land in a named report field, never pass and never raise;
* hypothesis properties — a flipped bit anywhere in any signed payload never
  verifies, and arbitrary attacker-signed statements or manifest values are reported,
  not crashed on;
* a few ``nova verify`` end-to-end runs that pin the operator-facing wording and that
  a failure is a clean exit 1, not a traceback (``CliRunner`` maps both to exit 1).

The bundle is exported once per module (a real capture subprocess plus an export is
the slow part); every test works on its own copy of the members.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from typer.testing import CliRunner, Result

from novafabric.capture.orchestrator import CaptureOrchestrator
from novafabric.cli.main import app
from novafabric.evidence.bundle import PREDICATE_RUN, _canonical_manifest_hash
from novafabric.evidence.bundle_verify import BundleSignatureReport, verify_bundle_signatures
from novafabric.evidence.intoto import DSSE_PAYLOAD_TYPE, dsse_sign_payload
from novafabric.evidence.signing import LocalSigner, generate_keypair

runner = CliRunner()
RUN_ATT = "attestations/run.intoto.json"
SIGNED_ATTESTATIONS = (
    RUN_ATT,
    "attestations/redaction.intoto.json",
    "attestations/lineage.intoto.json",
)
FAILURE_FIELDS = (
    "unsigned",
    "bad_signatures",
    "bad_subjects",
    "signer_not_trusted",
    "errors",
)

Files = dict[str, bytes]
Manifest = dict[str, Any]
Edit = Callable[[Files, Manifest, LocalSigner], None]

# Every property shares one exported bundle; the fixture is module-scoped on purpose.
PROPERTY = settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda inner: st.lists(inner, max_size=4) | st.dictionaries(st.text(), inner, max_size=4),
    max_leaves=12,
)


@dataclass(frozen=True)
class Exported:
    zip_path: Path
    public_key: Path
    files: Files
    manifest: Manifest

    def copy(self) -> tuple[Files, Manifest]:
        return dict(self.files), copy.deepcopy(self.manifest)


@pytest.fixture(scope="module")
def exported(tmp_path_factory: pytest.TempPathFactory) -> Exported:
    """One real exported bundle, its members, and its signer's public key."""
    tmp = tmp_path_factory.mktemp("exported")
    capsule = CaptureOrchestrator(base_dir=tmp / "runs").run(
        command=[sys.executable, "-c", "pass"]
    ).capsule_dir
    priv, pub = generate_keypair(tmp / "keys")
    out = tmp / "ev.zip"
    result = runner.invoke(
        app, ["export-evidence", str(capsule), "--key", str(priv), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output
    with zipfile.ZipFile(out) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    return Exported(out, pub, files, json.loads(files["manifest.json"]))


@pytest.fixture(scope="module")
def attacker(tmp_path_factory: pytest.TempPathFactory) -> LocalSigner:
    priv, _ = generate_keypair(tmp_path_factory.mktemp("attacker"))
    return LocalSigner(priv)


# ── helpers ────────────────────────────────────────────────────────────────────


def _repair(files: Files, manifest: Manifest, *, manifest_hash: bool = True) -> None:
    """Write every member's real digest (and optionally manifest_hash) back."""
    for art in manifest["artifacts"]:
        if art["path"] in files:
            art["sha256"] = "sha256:" + hashlib.sha256(files[art["path"]]).hexdigest()
    if manifest_hash:
        manifest["manifest_hash"] = _canonical_manifest_hash(manifest)


def _check(files: Files, manifest: Manifest, trusted: bytes | None = None) -> BundleSignatureReport:
    return verify_bundle_signatures(manifest, files.__getitem__, set(files), trusted)


def _failures(report: BundleSignatureReport) -> dict[str, list[str]]:
    found = {name: getattr(report, name) for name in FAILURE_FIELDS if getattr(report, name)}
    if not report.manifest_hash_ok:
        found["manifest_hash_ok"] = ["False"]
    return found


def _statement(files: Files, path: str) -> dict[str, Any]:
    statement: dict[str, Any] = json.loads(base64.b64decode(json.loads(files[path])["payload"]))
    return statement


def _sign_into(
    files: Files, manifest: Manifest, path: str, payload: bytes, signer: LocalSigner
) -> None:
    """Replace one attestation with ``payload`` signed by ``signer`` — envelope, .sig, .cert."""
    env = dsse_sign_payload(payload, DSSE_PAYLOAD_TYPE, signer)
    entry = next(s for s in manifest["signatures"] if s["envelope_path"] == path)
    files[path] = json.dumps(env).encode()
    files[entry["signature_path"]] = base64.b64decode(env["signatures"][0]["sig"])
    files[entry["certificate_path"]] = signer.public_pem


def _resign_all(files: Files, manifest: Manifest, signer: LocalSigner) -> None:
    for path in SIGNED_ATTESTATIONS:
        _sign_into(files, manifest, path, json.dumps(_statement(files, path)).encode(), signer)


def _write_zip(files: Files, manifest: Manifest, dst: Path) -> Path:
    files = {**files, "manifest.json": json.dumps(manifest, indent=2).encode()}
    with zipfile.ZipFile(dst, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return dst


def _verify(path: Path, *extra: str) -> Result:
    return runner.invoke(app, ["verify", str(path), *extra])


def _assert_clean_failure(result: Result) -> None:
    """Exit 1 *by the verifier's own decision* — CliRunner also exits 1 on a traceback."""
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)


# ── the bundle under test ──────────────────────────────────────────────────────


def test_the_exported_bundle_signs_exactly_the_attestations_these_tests_tamper_with(
    exported: Exported,
) -> None:
    listed = {a["path"] for a in exported.manifest["attestations"]}
    signed = {s["envelope_path"] for s in exported.manifest["signatures"]}
    assert listed == signed == set(SIGNED_ATTESTATIONS)


def test_the_intact_bundle_verifies_unpinned_and_pinned(exported: Exported) -> None:
    files, manifest = exported.copy()
    report = _check(files, manifest)
    assert report.ok, _failures(report)
    assert report.attestations_checked == len(SIGNED_ATTESTATIONS)
    assert len(report.signer_fingerprints) == 1
    pinned = _check(files, manifest, exported.public_key.read_bytes())
    assert pinned.ok, _failures(pinned)


# ── mutation matrix ────────────────────────────────────────────────────────────


def _zero_run_subject_unsigned(files: Files, _m: Manifest, _a: LocalSigner) -> None:
    env = json.loads(files[RUN_ATT])
    stmt = json.loads(base64.b64decode(env["payload"]))
    stmt["subject"][0]["digest"]["sha256"] = "0" * 64
    env["payload"] = base64.b64encode(json.dumps(stmt).encode()).decode()
    files[RUN_ATT] = json.dumps(env).encode()


def _drop_signature_entry(_f: Files, manifest: Manifest, _a: LocalSigner) -> None:
    manifest["signatures"] = [s for s in manifest["signatures"] if s["envelope_path"] != RUN_ATT]


def _delete(member: str) -> Edit:
    def edit(files: Files, _m: Manifest, _a: LocalSigner) -> None:
        del files[member]

    return edit


def _replace(member: str, data: bytes) -> Edit:
    def edit(files: Files, _m: Manifest, _a: LocalSigner) -> None:
        files[member] = data

    return edit


def _cert_is_attacker_key(files: Files, _m: Manifest, attacker: LocalSigner) -> None:
    files["signatures/run.cert"] = attacker.public_pem  # envelope still signed by the original


def _cert_is_not_ed25519(files: Files, _m: Manifest, _a: LocalSigner) -> None:
    files["signatures/run.cert"] = (
        ec.generate_private_key(ec.SECP256R1())
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )


def _cert_with_flipped_oid(files: Files, _m: Manifest, _a: LocalSigner) -> None:
    """1.3.101.112 (Ed25519) → 1.3.100.112: parses as DER, names an unknown algorithm."""
    der = bytearray(
        serialization.load_pem_public_key(files["signatures/run.cert"]).public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    der[7] ^= 0x01
    body = base64.encodebytes(bytes(der))
    files["signatures/run.cert"] = b"-----BEGIN PUBLIC KEY-----\n" + body + b"-----END PUBLIC KEY-----\n"


def _envelope_field(key: str, value: Any) -> Edit:
    def edit(files: Files, _m: Manifest, _a: LocalSigner) -> None:
        env = json.loads(files[RUN_ATT])
        env[key] = value
        files[RUN_ATT] = json.dumps(env).encode()

    return edit


def _manifest_entry(key: str, value: Any) -> Edit:
    def edit(_f: Files, manifest: Manifest, _a: LocalSigner) -> None:
        entry = next(a for a in manifest["attestations"] if a["path"] == RUN_ATT)
        entry[key] = value

    return edit


def _manifest_field(key: str, value: Any) -> Edit:
    def edit(_f: Files, manifest: Manifest, _a: LocalSigner) -> None:
        if value is _DELETE:
            del manifest[key]
        else:
            manifest[key] = value

    return edit


_DELETE = object()


def _signed_run_statement(payload: Any, *, retarget_manifest: bool = False) -> Edit:
    """The attacker signs ``payload`` as the run attestation with their own key."""

    def edit(files: Files, manifest: Manifest, attacker: LocalSigner) -> None:
        _sign_into(files, manifest, RUN_ATT, json.dumps(payload).encode(), attacker)
        if retarget_manifest:
            entry = next(a for a in manifest["attestations"] if a["path"] == RUN_ATT)
            entry["subject_hashes"] = [
                "sha256:" + s["digest"]["sha256"] for s in payload["subject"]
            ]

    return edit


def _run_statement_for(digest: str | None) -> dict[str, Any]:
    subject = [] if digest is None else [{"name": "run-capsule", "digest": {"sha256": digest}}]
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": subject,
        "predicateType": PREDICATE_RUN,
        "predicate": {},
    }


MUTATIONS = [
    # step 3 — signatures
    pytest.param(_zero_run_subject_unsigned, "bad_signatures", id="payload-edited-not-resigned"),
    pytest.param(_drop_signature_entry, "unsigned", id="signatures-entry-dropped"),
    pytest.param(_delete("signatures/run.cert"), "bad_signatures", id="certificate-deleted"),
    pytest.param(_delete("signatures/run.sig"), "bad_signatures", id="detached-sig-deleted"),
    pytest.param(_replace("signatures/run.sig", b"\0" * 64), "bad_signatures", id="detached-sig-differs"),
    pytest.param(_replace(RUN_ATT, b"\xff\xfe not json"), "bad_signatures", id="envelope-not-json"),
    pytest.param(_replace(RUN_ATT, b"[1, 2]"), "bad_signatures", id="envelope-is-a-list"),
    pytest.param(_envelope_field("signatures", []), "bad_signatures", id="envelope-has-no-signatures"),
    pytest.param(_envelope_field("signatures", [{"sig": "!!not base64!!"}]), "bad_signatures", id="sig-not-base64"),
    pytest.param(_envelope_field("payloadType", "text/plain"), "bad_signatures", id="payload-type-swapped"),
    pytest.param(_cert_is_attacker_key, "bad_signatures", id="certificate-swapped"),
    pytest.param(_cert_is_not_ed25519, "bad_signatures", id="certificate-not-ed25519"),
    pytest.param(_cert_with_flipped_oid, "bad_signatures", id="certificate-unknown-algorithm"),
    # step 4 — subjects
    pytest.param(_manifest_entry("predicate_type", "https://evil.example/v0"), "bad_subjects", id="predicate-type-relabelled"),
    pytest.param(_manifest_entry("subject_hashes", ["sha256:" + "ab" * 32]), "bad_subjects", id="subject-hashes-changed"),
    pytest.param(_manifest_entry("subject_hashes", "abc"), "bad_subjects", id="subject-hashes-not-a-list"),
    pytest.param(_signed_run_statement(_run_statement_for("ab" * 32), retarget_manifest=True), "bad_subjects", id="run-attestation-for-another-capsule"),
    pytest.param(_signed_run_statement(_run_statement_for(None), retarget_manifest=True), "bad_subjects", id="run-attestation-with-no-subject"),
    pytest.param(_manifest_field("subject", {"kind": "run-capsule", "capsule_hash": "sha256:" + "ab" * 32}), "bad_subjects", id="manifest-subject-retargeted"),
    pytest.param(_manifest_field("subject", "run-capsule"), "bad_subjects", id="manifest-subject-not-an-object"),
    # malformed but validly signed statements — the attacker holds a key the bundle accepts
    pytest.param(_signed_run_statement([1, 2]), "bad_subjects", id="signed-statement-is-a-list"),
    pytest.param(_signed_run_statement("statement"), "bad_subjects", id="signed-statement-is-a-string"),
    pytest.param(_signed_run_statement({**_run_statement_for("ab" * 32), "subject": [{"digest": "x"}]}), "bad_subjects", id="signed-subject-digest-not-an-object"),
    pytest.param(_signed_run_statement({**_run_statement_for("ab" * 32), "subject": "abc"}), "bad_subjects", id="signed-subject-not-a-list"),
    # step 1 and manifest shape
    pytest.param(_manifest_field("manifest_hash", _DELETE), "errors", id="manifest-hash-removed"),
    pytest.param(_manifest_field("attestations", []), "errors", id="no-attestations"),
    pytest.param(_manifest_field("attestations", {"path": RUN_ATT}), "errors", id="attestations-not-a-list"),
    pytest.param(_manifest_field("signatures", "x"), "unsigned", id="signatures-not-a-list"),
    pytest.param(lambda f, m, a: m["attestations"].insert(0, "x"), "errors", id="attestation-entry-not-an-object"),
    pytest.param(_manifest_entry("path", [RUN_ATT]), "errors", id="attestation-path-not-a-string"),
]


@pytest.mark.parametrize(("edit", "field"), MUTATIONS)
def test_every_tamper_is_reported_in_its_field_never_passed_never_raised(
    exported: Exported, attacker: LocalSigner, edit: Edit, field: str
) -> None:
    files, manifest = exported.copy()
    edit(files, manifest, attacker)
    _repair(files, manifest, manifest_hash="manifest_hash" in manifest)
    report = _check(files, manifest)
    assert not report.ok
    assert field in _failures(report), _failures(report)


def test_a_stale_manifest_hash_is_reported(exported: Exported) -> None:
    files, manifest = exported.copy()
    files["README.md"] += b"\nedited\n"
    _repair(files, manifest, manifest_hash=False)
    report = _check(files, manifest)
    assert not report.manifest_hash_ok
    assert not report.ok


@pytest.mark.parametrize("path", SIGNED_ATTESTATIONS)
def test_every_attestation_is_verified_not_just_the_run_one(
    exported: Exported, attacker: LocalSigner, path: str
) -> None:
    files, manifest = exported.copy()
    stmt = _statement(files, path)
    stmt["predicate"] = {"forged": True}
    env = json.loads(files[path])
    env["payload"] = base64.b64encode(json.dumps(stmt).encode()).decode()
    files[path] = json.dumps(env).encode()
    _repair(files, manifest)
    report = _check(files, manifest)
    assert [b.split(" ")[0] for b in report.bad_signatures] == [path]


def test_a_bundle_resigned_by_another_key_fails_only_when_the_signer_is_pinned(
    exported: Exported, attacker: LocalSigner
) -> None:
    files, manifest = exported.copy()
    _resign_all(files, manifest, attacker)
    _repair(files, manifest)
    unpinned = _check(files, manifest)
    assert unpinned.ok, _failures(unpinned)  # internally consistent, proves no identity
    pinned = _check(files, manifest, exported.public_key.read_bytes())
    assert sorted(pinned.signer_not_trusted) == sorted(SIGNED_ATTESTATIONS)
    assert not pinned.ok


def test_one_attestation_by_a_second_signer_is_caught_by_the_pin(
    exported: Exported, attacker: LocalSigner
) -> None:
    files, manifest = exported.copy()
    _sign_into(files, manifest, RUN_ATT, json.dumps(_statement(files, RUN_ATT)).encode(), attacker)
    _repair(files, manifest)
    unpinned = _check(files, manifest)
    assert unpinned.ok
    assert len(unpinned.signer_fingerprints) == 2  # the report shows the mix …
    pinned = _check(files, manifest, exported.public_key.read_bytes())
    assert pinned.signer_not_trusted == [RUN_ATT]  # … and the pin names the odd one out


# ── properties ─────────────────────────────────────────────────────────────────


@PROPERTY
@given(
    path=st.sampled_from(SIGNED_ATTESTATIONS),
    where=st.floats(min_value=0, max_value=1, exclude_max=True),
    mask=st.integers(min_value=1, max_value=255),
)
def test_no_single_byte_change_to_a_signed_payload_verifies(
    exported: Exported, path: str, where: float, mask: int
) -> None:
    files, manifest = exported.copy()
    env = json.loads(files[path])
    payload = bytearray(base64.b64decode(env["payload"]))
    payload[int(where * len(payload))] ^= mask
    env["payload"] = base64.b64encode(bytes(payload)).decode()
    files[path] = json.dumps(env).encode()
    _repair(files, manifest)
    report = _check(files, manifest)
    assert not report.ok
    assert any(b.startswith(path) for b in report.bad_signatures), _failures(report)


@PROPERTY
@given(payload=JSON)
def test_an_arbitrary_attacker_signed_statement_is_reported_not_crashed_on(
    exported: Exported, attacker: LocalSigner, payload: Any
) -> None:
    files, manifest = exported.copy()
    _sign_into(files, manifest, RUN_ATT, json.dumps(payload).encode(), attacker)
    _repair(files, manifest)
    report = _check(files, manifest)
    assert not report.ok
    assert report.bad_subjects or report.errors, _failures(report)


@PROPERTY
@given(
    target=st.sampled_from(
        [("manifest", k) for k in ("attestations", "signatures", "subject", "manifest_hash")]
        + [("entry", k) for k in ("path", "subject_hashes", "predicate_type")]
    ),
    value=JSON,
)
def test_an_arbitrary_manifest_value_is_reported_not_crashed_on(
    exported: Exported, target: tuple[str, str], value: Any
) -> None:
    scope, key = target
    files, manifest = exported.copy()
    holder = manifest if scope == "manifest" else manifest["attestations"][0]
    assume(holder.get(key) != value)
    holder[key] = value
    _repair(files, manifest, manifest_hash=key != "manifest_hash")
    report = _check(files, manifest)
    if key == "predicate_type" and not value:
        return  # an absent label is not checked; the signed predicateType still governs
    assert not report.ok, f"{scope}.{key}={value!r} verified"


# ── nova verify end to end ─────────────────────────────────────────────────────


def test_cli_an_intact_exported_bundle_passes_and_says_the_signer_is_unpinned(
    exported: Exported,
) -> None:
    result = _verify(exported.zip_path)
    assert result.exit_code == 0, result.output
    assert "signer not pinned" in result.output
    pinned = _verify(exported.zip_path, "--public-key", str(exported.public_key))
    assert pinned.exit_code == 0, pinned.output
    assert "Signer is the --public-key: OK" in pinned.output


def test_cli_a_forged_signed_payload_fails_even_with_digests_and_hash_repaired(
    exported: Exported, attacker: LocalSigner, tmp_path: Path
) -> None:
    files, manifest = exported.copy()
    _zero_run_subject_unsigned(files, manifest, attacker)
    _repair(files, manifest)
    result = _verify(_write_zip(files, manifest, tmp_path / "f.zip"))
    _assert_clean_failure(result)
    assert "bad signature" in result.output
    assert RUN_ATT in result.output


def test_cli_a_stale_manifest_hash_fails(exported: Exported, tmp_path: Path) -> None:
    files, manifest = exported.copy()
    files["README.md"] += b"\nedited\n"
    _repair(files, manifest, manifest_hash=False)
    result = _verify(_write_zip(files, manifest, tmp_path / "s.zip"))
    _assert_clean_failure(result)
    assert "manifest_hash matches the manifest: FAIL" in result.output


def test_cli_a_resigned_bundle_fails_only_against_the_pinned_key(
    exported: Exported, attacker: LocalSigner, tmp_path: Path
) -> None:
    files, manifest = exported.copy()
    _resign_all(files, manifest, attacker)
    _repair(files, manifest)
    resigned = _write_zip(files, manifest, tmp_path / "r.zip")
    unpinned = _verify(resigned)
    assert unpinned.exit_code == 0, unpinned.output
    assert "signer not pinned" in unpinned.output
    pinned = _verify(resigned, "--public-key", str(exported.public_key))
    _assert_clean_failure(pinned)
    assert "Signer is the --public-key: FAIL" in pinned.output


def test_cli_a_run_attestation_for_another_capsule_fails(
    exported: Exported, attacker: LocalSigner, tmp_path: Path
) -> None:
    files, manifest = exported.copy()
    _signed_run_statement(_run_statement_for("ab" * 32), retarget_manifest=True)(
        files, manifest, attacker
    )
    _repair(files, manifest)
    result = _verify(_write_zip(files, manifest, tmp_path / "o.zip"))
    _assert_clean_failure(result)
    assert "run subject" in result.output


def test_cli_a_malformed_signed_statement_fails_cleanly(
    exported: Exported, attacker: LocalSigner, tmp_path: Path
) -> None:
    files, manifest = exported.copy()
    _signed_run_statement([1, 2])(files, manifest, attacker)
    _repair(files, manifest)
    result = _verify(_write_zip(files, manifest, tmp_path / "m.zip"))
    _assert_clean_failure(result)
    assert "subject" in result.output
