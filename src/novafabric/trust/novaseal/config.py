"""NovaSeal v0.1 signing configuration.

Auto-discovery order:
  1. NOVAFABRIC_SEAL_CONFIG environment variable (path to YAML)
  2. ~/.novafabric/novaseal.yaml
  3. None — NovaSeal disabled

novaseal.yaml schema (local profile):

    profile: local
    key_path: /path/to/ecdsa-p256.pem
    cert_path: /path/to/cert.pem
    tsa_url: https://freetsa.org/tsr   # optional; omit to skip timestamps
    merkle_db: /path/to/merkle.db      # optional; defaults to ~/.novafabric/merkle.db

novaseal.yaml schema (aws_kms profile):

    profile: aws_kms
    kms_key_id: arn:aws:kms:us-east-1:123456789012:key/mrk-...
    aws_region: us-east-1              # optional; default us-east-1
    cert_path: /path/to/cert.pem       # cert exported for the KMS key
    tsa_url: https://freetsa.org/tsr   # optional
    merkle_db: /path/to/merkle.db      # optional

novaseal.yaml schema (azure_kv profile):

    profile: azure_kv
    vault_url: https://myvault.vault.azure.net/
    key_name: my-ec-key
    cert_path: /path/to/cert.pem
    tsa_url: https://freetsa.org/tsr   # optional
    merkle_db: /path/to/merkle.db      # optional

novaseal.yaml schema (gcp_kms profile):

    profile: gcp_kms
    key_version_name: projects/P/locations/L/keyRings/R/cryptoKeys/K/cryptoKeyVersions/1
    cert_path: /path/to/cert.pem
    tsa_url: https://freetsa.org/tsr   # optional
    merkle_db: /path/to/merkle.db      # optional
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from novafabric.trust.novaseal.signing_backend import SigningBackend

try:
    import yaml  # already a runtime dep via capture/orchestrator
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

_DEFAULT_TSA_URL = "https://freetsa.org/tsr"
_DEFAULT_MERKLE_DB = Path.home() / ".novafabric" / "novaseal-merkle.db"
_DEFAULT_CONFIG_PATH = Path.home() / ".novafabric" / "novaseal.yaml"

_SUPPORTED_PROFILES = frozenset({"local", "aws_kms", "azure_kv", "gcp_kms"})


@dataclass
class SigningProfile:
    """Parsed signing configuration for NovaSeal.

    Profiles:
        ``"local"``    — ECDSA P-256 private key loaded from *key_path*.
        ``"aws_kms"``  — AWS KMS asymmetric key (ECDSA_SHA_256).
        ``"azure_kv"`` — Azure Key Vault EC P-256 key.
        ``"gcp_kms"``  — GCP Cloud KMS asymmetric EC P-256 key.
    """

    profile: str  # "local" | "aws_kms" | "azure_kv" | "gcp_kms"

    # ---- local profile fields ----
    key_path: Optional[Path] = None
    cert_path: Optional[Path] = None

    # ---- aws_kms fields ----
    kms_key_id: Optional[str] = None     # KMS key ARN or alias
    aws_region: str = "us-east-1"

    # ---- azure_kv fields ----
    vault_url: Optional[str] = None
    key_name: Optional[str] = None

    # ---- gcp_kms fields ----
    key_version_name: Optional[str] = None

    # ---- shared fields ----
    tsa_url: str = _DEFAULT_TSA_URL
    merkle_db: Path = field(default_factory=lambda: _DEFAULT_MERKLE_DB)


class SealConfigError(Exception):
    """Raised when novaseal.yaml is missing or malformed."""


def resolve_merkle_db_uri() -> str:
    """Canonical Merkle DB URI resolution — returns a string (path or DSN).

    Precedence:
    1. ``NOVAFABRIC_SEAL_DB_PATH`` env var (explicit override / test escape hatch).
       May be a file path **or** a ``postgresql://…`` DSN (Scale-S4).
    2. ``merkle_db`` field in the discovered ``novaseal.yaml``.
    3. ``~/.novafabric/novaseal-merkle.db`` default (SQLite).
    """
    env_db = os.environ.get("NOVAFABRIC_SEAL_DB_PATH")
    if env_db:
        return env_db

    env_cfg = os.environ.get("NOVAFABRIC_SEAL_CONFIG")
    config_path = Path(env_cfg) if env_cfg else _DEFAULT_CONFIG_PATH
    if config_path.exists() and yaml is not None:
        try:
            with open(config_path) as f:
                raw = yaml.safe_load(f)
            if isinstance(raw, dict) and "merkle_db" in raw:
                val = str(raw["merkle_db"])
                if val.startswith(("postgresql://", "postgres://")):
                    return val
                return str(Path(val).expanduser())
        except Exception:  # noqa: BLE001
            pass  # malformed yaml — fall through

    return str(_DEFAULT_MERKLE_DB)


def resolve_merkle_db_path() -> Path:
    """Backward-compatible wrapper — returns a ``Path`` for file-based configs.

    For Postgres DSNs use ``resolve_merkle_db_uri()`` instead.  This function
    raises ``ValueError`` if the configured URI is a ``postgresql://`` DSN.
    """
    uri = resolve_merkle_db_uri()
    if uri.startswith(("postgresql://", "postgres://")):
        raise ValueError(
            f"NOVAFABRIC_SEAL_DB_PATH is a Postgres DSN ({uri!r}); "
            "use resolve_merkle_db_uri() and open_merkle_log() instead."
        )
    return Path(uri)


def load_signing_profile() -> Optional[SigningProfile]:
    """Return a SigningProfile if NovaSeal is configured, else None."""
    env_path = os.environ.get("NOVAFABRIC_SEAL_CONFIG")
    if env_path:
        config_path = Path(env_path)
        if not config_path.exists():
            raise SealConfigError(
                f"NOVAFABRIC_SEAL_CONFIG points to missing file: {config_path}"
            )
        return _parse_profile(config_path)

    if _DEFAULT_CONFIG_PATH.exists():
        return _parse_profile(_DEFAULT_CONFIG_PATH)

    return None


def _parse_profile(path: Path) -> SigningProfile:
    if yaml is None:  # pragma: no cover
        raise SealConfigError("PyYAML is required for novaseal config parsing")
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise SealConfigError(f"novaseal.yaml must be a YAML mapping: {path}")

    def req(key: str) -> str:
        if key not in raw:
            raise SealConfigError(f"novaseal.yaml missing required key: {key!r}")
        return str(raw[key])

    profile = raw.get("profile", "local")
    if profile not in _SUPPORTED_PROFILES:
        raise SealConfigError(
            f"novaseal profile {profile!r} is not supported "
            f"(supported: {sorted(_SUPPORTED_PROFILES)})"
        )

    tsa_url = str(raw.get("tsa_url", _DEFAULT_TSA_URL))
    merkle_db = Path(raw.get("merkle_db", str(_DEFAULT_MERKLE_DB))).expanduser()
    merkle_db.parent.mkdir(parents=True, exist_ok=True)

    if profile == "local":
        key_path = Path(req("key_path")).expanduser()
        cert_path = Path(req("cert_path")).expanduser()

        if not key_path.exists():
            raise SealConfigError(f"NovaSeal key_path not found: {key_path}")
        if not cert_path.exists():
            raise SealConfigError(f"NovaSeal cert_path not found: {cert_path}")

        return SigningProfile(
            profile=profile,
            key_path=key_path,
            cert_path=cert_path,
            tsa_url=tsa_url,
            merkle_db=merkle_db,
        )

    if profile == "aws_kms":
        kms_key_id = req("kms_key_id")
        aws_region = str(raw.get("aws_region", "us-east-1"))
        cert_path = Path(req("cert_path")).expanduser()
        if not cert_path.exists():
            raise SealConfigError(f"NovaSeal cert_path not found: {cert_path}")
        return SigningProfile(
            profile=profile,
            cert_path=cert_path,
            kms_key_id=kms_key_id,
            aws_region=aws_region,
            tsa_url=tsa_url,
            merkle_db=merkle_db,
        )

    if profile == "azure_kv":
        vault_url = req("vault_url")
        key_name = req("key_name")
        cert_path = Path(req("cert_path")).expanduser()
        if not cert_path.exists():
            raise SealConfigError(f"NovaSeal cert_path not found: {cert_path}")
        return SigningProfile(
            profile=profile,
            cert_path=cert_path,
            vault_url=vault_url,
            key_name=key_name,
            tsa_url=tsa_url,
            merkle_db=merkle_db,
        )

    # profile == "gcp_kms"
    key_version_name = req("key_version_name")
    cert_path = Path(req("cert_path")).expanduser()
    if not cert_path.exists():
        raise SealConfigError(f"NovaSeal cert_path not found: {cert_path}")
    return SigningProfile(
        profile=profile,
        cert_path=cert_path,
        key_version_name=key_version_name,
        tsa_url=tsa_url,
        merkle_db=merkle_db,
    )


def build_signing_backend(profile: SigningProfile) -> "SigningBackend":
    """Construct and return the appropriate ``SigningBackend`` for *profile*.

    Args:
        profile: A parsed ``SigningProfile``.

    Returns:
        A concrete backend that implements ``SigningBackend``.

    Raises:
        SealConfigError: if a required field is missing.
        ImportError:     if a cloud SDK is not installed.
    """
    from novafabric.trust.novaseal.signing_backend import (
        AwsKmsSigningBackend,
        AzureKvSigningBackend,
        GcpKmsSigningBackend,
        LocalSigningBackend,
    )

    if profile.profile == "local":
        if profile.key_path is None or profile.cert_path is None:
            raise SealConfigError("local profile requires key_path and cert_path")
        return LocalSigningBackend(profile.key_path, profile.cert_path)

    if profile.profile == "aws_kms":
        if profile.kms_key_id is None or profile.cert_path is None:
            raise SealConfigError("aws_kms profile requires kms_key_id and cert_path")
        return AwsKmsSigningBackend(
            key_id=profile.kms_key_id,
            cert_path=profile.cert_path,
            region=profile.aws_region,
        )

    if profile.profile == "azure_kv":
        if profile.vault_url is None or profile.key_name is None or profile.cert_path is None:
            raise SealConfigError(
                "azure_kv profile requires vault_url, key_name, and cert_path"
            )
        return AzureKvSigningBackend(
            vault_url=profile.vault_url,
            key_name=profile.key_name,
            cert_path=profile.cert_path,
        )

    if profile.profile == "gcp_kms":
        if profile.key_version_name is None or profile.cert_path is None:
            raise SealConfigError("gcp_kms profile requires key_version_name and cert_path")
        return GcpKmsSigningBackend(
            key_version_name=profile.key_version_name,
            cert_path=profile.cert_path,
        )

    raise SealConfigError(f"Unknown profile: {profile.profile!r}")
