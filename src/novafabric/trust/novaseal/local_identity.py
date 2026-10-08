"""Local sealing identity for first-run NovaSeal setup (ADR-0301, experimental).

``nova seal init`` calls :func:`init_local_identity`. It creates, under
``$NOVAFABRIC_HOME/keys/novaseal/``:

* ``signing.key.pem`` — a dedicated ECDSA P-256 signing key (mode 0600). It is *not*
  the ``nova init`` Ed25519 key: that key signs Evidence Bundle exports, the local
  profile and backup signing assume P-256, and ``nova init --force`` must not be able
  to break the sealing certificate (ADR-0301, fact 5);
* ``ca.key.pem`` / ``ca.crt.pem`` — a local seal CA (P-256, ``CA=TRUE, pathlen 0``).
  ``ca.crt.pem`` is the anchor a verifier can pin with ``nova verify --ca-bundle``.
  An Ed25519 CA would not do: the WebPKI path builder rejects Ed25519-signed
  certificates;
* ``signing.crt.pem`` — the leaf certificate for the signing key, issued by that CA
  (``CA=FALSE``), so the existing end-entity rule in ``x509_identity`` holds unchanged;

and writes ``$NOVAFABRIC_HOME/novaseal.yaml`` (``profile: local``, no ``tsa_url`` —
fully offline). Both certificates carry :data:`LOCAL_IDENTITY_ORG` in their subject so a
verifier can say *"self-asserted local key"* instead of implying a real identity.

The identity is **self-asserted**: it proves that the holder of the private key signed,
nothing about who that holder is. Nothing here opens a network connection.
"""

from __future__ import annotations

import datetime
import hashlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

#: Subject ``O=`` attribute carried by every certificate ``nova seal init`` issues.
LOCAL_IDENTITY_ORG = "NovaFabric local seal identity (self-asserted)"
#: First line of a ``novaseal.yaml`` written (and therefore replaceable) by ``nova seal init``.
MANAGED_MARKER = "# managed-by: nova seal init"

CA_VALIDITY_DAYS = 3650
LEAF_VALIDITY_DAYS = 1825
_CLOCK_SKEW = datetime.timedelta(minutes=5)

_KEY_MODE = stat.S_IRUSR | stat.S_IWUSR  # 0600
_DIR_MODE = stat.S_IRWXU  # 0700
_PUBLIC_MODE = 0o644


class LocalIdentityError(Exception):
    """``nova seal init`` cannot create or rotate the local sealing identity."""


class OperatorManagedConfigError(LocalIdentityError):
    """``novaseal.yaml`` exists and was not written by ``nova seal init``.

    It is never replaced: swapping an operator's KMS or CA-issued profile for a
    self-asserted local one would silently lower the trust level of every new seal.
    """


class BrokenManagedConfigError(LocalIdentityError):
    """A ``novaseal.yaml`` written by ``nova seal init`` no longer loads."""


class MissingCaKeyError(LocalIdentityError):
    """Rotation needs the local CA private key, and it is absent or unreadable."""


@dataclass(frozen=True)
class LocalIdentityPaths:
    """Where the local sealing identity lives under one ``NOVAFABRIC_HOME``."""

    home: Path

    @property
    def directory(self) -> Path:
        return self.home / "keys" / "novaseal"

    @property
    def signing_key(self) -> Path:
        return self.directory / "signing.key.pem"

    @property
    def signing_cert(self) -> Path:
        return self.directory / "signing.crt.pem"

    @property
    def ca_key(self) -> Path:
        return self.directory / "ca.key.pem"

    @property
    def ca_cert(self) -> Path:
        return self.directory / "ca.crt.pem"

    @property
    def archive_root(self) -> Path:
        return self.directory / "archive"

    @property
    def config(self) -> Path:
        return self.home / "novaseal.yaml"

    @property
    def merkle_db(self) -> Path:
        return self.home / "novaseal-merkle.db"


@dataclass(frozen=True)
class LocalIdentityResult:
    """What :func:`init_local_identity` did.

    ``status`` is one of ``"created"``, ``"rotated"``, ``"already-configured"`` (a
    managed, loadable config — nothing changed) or ``"operator-managed"`` (a config
    NovaFabric did not write — nothing changed).
    """

    status: str
    paths: LocalIdentityPaths
    profile: Optional[str] = None
    signing_key_reused: bool = False
    ca_reused: bool = False
    archive_dir: Optional[Path] = None
    old_keyid: Optional[str] = None
    new_keyid: Optional[str] = None
    leaf_not_after: Optional[datetime.datetime] = None
    ca_fingerprint: Optional[str] = None
    rotation_logged: bool = False
    rotation_log_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_managed_config(path: Path) -> bool:
    """True when *path* starts with :data:`MANAGED_MARKER` (written by ``nova seal init``)."""
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline().strip() == MANAGED_MARKER
    except (OSError, UnicodeDecodeError):
        return False


def keyid_of_cert_file(cert_path: Path) -> str:
    """SHA-256 of the certificate DER — the ``keyid`` NovaSeal writes into envelopes."""
    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def init_local_identity(
    home: Path,
    *,
    force: bool = False,
    new_ca: bool = False,
    now: Optional[datetime.datetime] = None,
) -> LocalIdentityResult:
    """Create, or with *force* rotate, the local sealing identity under *home*.

    Args:
        home: The ``NOVAFABRIC_HOME`` to configure.
        force: Rotate: archive the current signing key, leaf and config to
            ``keys/novaseal/archive/<UTC>/``, generate a new key and issue a new leaf
            from the **same** local CA (trust continuity for anyone who pinned
            ``ca.crt.pem``), and log a ``key_rotation`` entry in the Merkle log.
        new_ca: With *force*, also archive and regenerate the local CA. Breaks
            continuity for verifiers who pinned the old CA.
        now: Clock override for tests.

    Raises:
        LocalIdentityError: ``new_ca`` without ``force``.
        OperatorManagedConfigError: ``novaseal.yaml`` exists and was not written by
            ``nova seal init`` — reported with *force* too; never replaced.
        BrokenManagedConfigError: the managed config does not load and *force* is off.
        MissingCaKeyError: rotation without ``new_ca`` but the CA key is unusable.
    """
    if new_ca and not force:
        raise LocalIdentityError("--new-ca replaces the local seal CA; it requires --force")

    paths = LocalIdentityPaths(home)
    when = now or datetime.datetime.now(datetime.timezone.utc)

    if paths.config.exists():
        if not is_managed_config(paths.config):
            if force:
                raise OperatorManagedConfigError(
                    f"{paths.config} was not written by `nova seal init` (no "
                    f"'{MANAGED_MARKER}' first line). It is never replaced, even with "
                    "--force: move it away yourself if you really want a self-asserted "
                    "local identity instead."
                )
            return LocalIdentityResult(
                status="operator-managed",
                paths=paths,
                profile=_profile_name(paths.config),
            )
        load_error = _config_load_error(paths.config)
        if not force:
            if load_error is not None:
                raise BrokenManagedConfigError(
                    f"{paths.config} does not load: {load_error}. Run "
                    "`nova seal init --force` to archive it and issue a fresh identity."
                )
            return _describe_existing(paths)
        return _rotate(paths, when, new_ca=new_ca)

    return _create(paths, when)


# ---------------------------------------------------------------------------
# Create / rotate
# ---------------------------------------------------------------------------


def _create(paths: LocalIdentityPaths, when: datetime.datetime) -> LocalIdentityResult:
    _ensure_dir(paths.directory)

    ca_pair = _load_ca(paths)
    ca_reused = ca_pair is not None
    archive_dir: Optional[Path] = None
    if ca_pair is None:
        leftovers = [p for p in (paths.ca_key, paths.ca_cert) if p.exists()]
        if leftovers:  # an unusable half-CA: keep it, never overwrite it
            archive_dir = _archive(paths, when, leftovers)
        ca_pair = _write_new_ca(paths, when)
    ca_key, ca_cert = ca_pair

    signing_key = _load_signing_key(paths.signing_key)
    key_reused = signing_key is not None
    if signing_key is None:
        signing_key = ec.generate_private_key(ec.SECP256R1())
        _write_private_key(paths.signing_key, signing_key)
    else:
        _tighten_key_mode(paths.signing_key)

    leaf = _issue_leaf(signing_key, ca_key, ca_cert, when)
    _write_public(paths.signing_cert, leaf.public_bytes(serialization.Encoding.PEM))
    _write_config(paths)

    return LocalIdentityResult(
        status="created",
        paths=paths,
        profile="local",
        signing_key_reused=key_reused,
        ca_reused=ca_reused,
        archive_dir=archive_dir,
        new_keyid=keyid_of_cert_file(paths.signing_cert),
        leaf_not_after=leaf.not_valid_after_utc,
        ca_fingerprint=_fingerprint(ca_cert),
    )


def _rotate(
    paths: LocalIdentityPaths, when: datetime.datetime, *, new_ca: bool
) -> LocalIdentityResult:
    ca_pair = None if new_ca else _load_ca(paths)
    if not new_ca and ca_pair is None:
        raise MissingCaKeyError(
            f"the local seal CA ({paths.ca_key}, {paths.ca_cert}) is missing or unreadable, "
            "so a new leaf cannot be issued under it. Restore it, or pass --new-ca "
            "(verifiers who pinned the old CA will no longer validate new capsules)."
        )

    old_keyid: Optional[str] = None
    try:
        old_keyid = keyid_of_cert_file(paths.signing_cert)
    except (OSError, ValueError):
        old_keyid = None  # a broken managed config may have lost its leaf

    movable = [paths.signing_key, paths.signing_cert, paths.config]
    if new_ca:
        movable += [paths.ca_key, paths.ca_cert]
    archive_dir = _archive(paths, when, movable)

    if ca_pair is None:
        ca_pair = _write_new_ca(paths, when)
    ca_key, ca_cert = ca_pair

    signing_key = ec.generate_private_key(ec.SECP256R1())
    _write_private_key(paths.signing_key, signing_key)
    leaf = _issue_leaf(signing_key, ca_key, ca_cert, when)
    _write_public(paths.signing_cert, leaf.public_bytes(serialization.Encoding.PEM))
    _write_config(paths)
    new_keyid = keyid_of_cert_file(paths.signing_cert)

    logged, log_error = _log_rotation(paths.merkle_db, old_keyid, new_keyid, new_ca=new_ca)
    return LocalIdentityResult(
        status="rotated",
        paths=paths,
        profile="local",
        ca_reused=not new_ca,
        archive_dir=archive_dir,
        old_keyid=old_keyid,
        new_keyid=new_keyid,
        leaf_not_after=leaf.not_valid_after_utc,
        ca_fingerprint=_fingerprint(ca_cert),
        rotation_logged=logged,
        rotation_log_error=log_error,
    )


def _describe_existing(paths: LocalIdentityPaths) -> LocalIdentityResult:
    keyid: Optional[str] = None
    not_after: Optional[datetime.datetime] = None
    ca_fp: Optional[str] = None
    try:
        leaf = x509.load_pem_x509_certificate(paths.signing_cert.read_bytes())
        keyid = keyid_of_cert_file(paths.signing_cert)
        not_after = leaf.not_valid_after_utc
    except (OSError, ValueError):
        pass
    try:
        ca_fp = _fingerprint(x509.load_pem_x509_certificate(paths.ca_cert.read_bytes()))
    except (OSError, ValueError):
        pass
    return LocalIdentityResult(
        status="already-configured",
        paths=paths,
        profile="local",
        new_keyid=keyid,
        leaf_not_after=not_after,
        ca_fingerprint=ca_fp,
    )


def _archive(paths: LocalIdentityPaths, when: datetime.datetime, movable: list[Path]) -> Path:
    """Move the files a rotation replaces into ``archive/<UTC>/`` (never delete them)."""
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    target = paths.archive_root / stamp
    suffix = 1
    while target.exists():
        suffix += 1
        target = paths.archive_root / f"{stamp}-{suffix}"
    _ensure_dir(paths.archive_root)
    _ensure_dir(target)
    for source in movable:
        if source.exists():
            shutil.move(str(source), str(target / source.name))
    return target


def _log_rotation(
    merkle_db: Path, old_keyid: Optional[str], new_keyid: str, *, new_ca: bool
) -> tuple[bool, Optional[str]]:
    """Append a ``key_rotation`` entry; a log fault is reported, never raised."""
    from novafabric.trust.novaseal.merkle import MerkleError, open_merkle_log

    entry: dict[str, object] = {
        "event": "key_rotation",
        "old_keyid": old_keyid or "unknown",
        "new_keyid": new_keyid,
        "source": "nova seal init --force",
        "ca_replaced": new_ca,
    }
    try:
        log = open_merkle_log(merkle_db)
        try:
            log.append(entry)
        finally:
            log.close()
    except (MerkleError, OSError) as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 — sqlite3 errors; rotation itself succeeded
        return False, str(exc)
    return True, None


# ---------------------------------------------------------------------------
# Key / certificate helpers
# ---------------------------------------------------------------------------


def _short_fp(public_key: ec.EllipticCurvePublicKey) -> str:
    der = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(der).hexdigest()[:12]


def _name(common_name: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, LOCAL_IDENTITY_ORG),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )


def _key_usage(*, signer: bool) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=signer,
        content_commitment=signer,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=not signer,
        crl_sign=not signer,
        encipher_only=False,
        decipher_only=False,
    )


def _write_new_ca(
    paths: LocalIdentityPaths, when: datetime.datetime
) -> tuple[ec.EllipticCurvePrivateKey, x509.Certificate]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = _name(f"NovaFabric local seal CA {_short_fp(key.public_key())}")
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(when - _CLOCK_SKEW)
        .not_valid_after(when + datetime.timedelta(days=CA_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(signer=False), critical=True)
        .add_extension(ski, critical=False)
        .sign(key, hashes.SHA256())
    )
    _write_private_key(paths.ca_key, key)
    _write_public(paths.ca_cert, cert.public_bytes(serialization.Encoding.PEM))
    return key, cert


def _issue_leaf(
    signing_key: ec.EllipticCurvePrivateKey,
    ca_key: ec.EllipticCurvePrivateKey,
    ca_cert: x509.Certificate,
    when: datetime.datetime,
) -> x509.Certificate:
    not_after = min(
        when + datetime.timedelta(days=LEAF_VALIDITY_DAYS), ca_cert.not_valid_after_utc
    )
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    return (
        x509.CertificateBuilder()
        .subject_name(_name(f"NovaFabric local signer {_short_fp(signing_key.public_key())}"))
        .issuer_name(ca_cert.subject)
        .public_key(signing_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(when - _CLOCK_SKEW)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(signer=True), critical=True)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(signing_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )


def _load_ec_key(path: Path) -> Optional[ec.EllipticCurvePrivateKey]:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None
    if isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1):
        return key
    return None


def _load_signing_key(path: Path) -> Optional[ec.EllipticCurvePrivateKey]:
    """Reuse a P-256 key left in place (e.g. the config was deleted); else ``None``."""
    if not path.exists():
        return None
    key = _load_ec_key(path)
    if key is None:
        raise LocalIdentityError(
            f"{path} exists but is not an unencrypted ECDSA P-256 private key; move it "
            "away or run `nova seal init --force`"
        )
    return key


def _load_ca(
    paths: LocalIdentityPaths,
) -> Optional[tuple[ec.EllipticCurvePrivateKey, x509.Certificate]]:
    """The existing local CA, if its key and certificate load and belong together."""
    key = _load_ec_key(paths.ca_key)
    if key is None:
        return None
    try:
        cert = x509.load_pem_x509_certificate(paths.ca_cert.read_bytes())
    except (OSError, ValueError):
        return None
    if _spki(cert.public_key()) != _spki(key.public_key()):
        return None
    return key, cert


def _spki(public_key: object) -> bytes:
    return public_key.public_bytes(  # type: ignore[attr-defined, no-any-return]
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _fingerprint(cert: x509.Certificate) -> str:
    return "sha256:" + cert.fingerprint(hashes.SHA256()).hex()


# ---------------------------------------------------------------------------
# File helpers — keys are created 0600 and renamed into place, never chmod-ed after
# ---------------------------------------------------------------------------


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(_DIR_MODE)


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)  # O_CREAT mode is filtered by the umask
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_private_key(path: Path, key: ec.EllipticCurvePrivateKey) -> None:
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _atomic_write(path, pem, _KEY_MODE)


def _write_public(path: Path, data: bytes) -> None:
    _atomic_write(path, data, _PUBLIC_MODE)


def _tighten_key_mode(path: Path) -> None:
    if path.stat().st_mode & 0o077:
        path.chmod(_KEY_MODE)


def _write_config(paths: LocalIdentityPaths) -> None:
    text = (
        f"{MANAGED_MARKER}\n"
        "# Local sealing identity (ADR-0301). SELF-ASSERTED: a seal made with this key\n"
        "# proves the holder of the key signed; it does not identify a person or an\n"
        "# organisation. Rotate with `nova seal init --force`. Pin the local CA with\n"
        f"#   nova verify --ca-bundle {paths.ca_cert} <capsule>\n"
        "# No tsa_url: sealing makes no network call (ADR-0292).\n"
        "profile: local\n"
        f"key_path: {paths.signing_key}\n"
        f"cert_path: {paths.signing_cert}\n"
        f"merkle_db: {paths.merkle_db}\n"
    )
    _atomic_write(paths.config, text.encode("utf-8"), _PUBLIC_MODE)


def _config_load_error(path: Path) -> Optional[str]:
    """``None`` when *path* parses as a signing profile, else the reason it does not."""
    from novafabric.trust.novaseal.config import SealConfigError, _parse_profile

    try:
        _parse_profile(path)
    except SealConfigError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 — YAML errors, unreadable file
        return f"{type(exc).__name__}: {exc}"
    return None


def _profile_name(path: Path) -> Optional[str]:
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — description only
        return None
    if isinstance(raw, dict):
        return str(raw.get("profile", "local"))
    return None
