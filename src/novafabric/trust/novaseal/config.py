"""NovaSeal v0.1 signing configuration.

Auto-discovery order:
  1. NOVAFABRIC_SEAL_CONFIG environment variable (path to YAML)
  2. $NOVAFABRIC_HOME/novaseal.yaml (~/.novafabric/novaseal.yaml when NOVAFABRIC_HOME is unset)
  3. None — NovaSeal disabled

novaseal.yaml schema (local profile):

    profile: local
    key_path: /path/to/ecdsa-p256.pem
    cert_path: /path/to/cert.pem
    tsa_url: https://tsa.example.org/tsr  # optional; omit (or leave empty) for
                                          # no RFC 3161 timestamp and no network call
    tsa_urls:                          # optional (REG-ADR-007); ordered TSA
      - https://tsa.example-primary.com/tsr    # fallback list. Defaults to
      - https://tsa.example-backup.com/tsr     # [tsa_url] when omitted; on its own
                                               # it also opts in (tsa_url = [0]).

Timestamping is **opt-in** (ADR-0292): there is no default TSA. NovaFabric never
contacts a timestamp authority unless ``tsa_url`` or ``tsa_urls`` names one. The
public FreeTSA service (``https://freetsa.org/tsr``) is a usable example for
development; it is not eIDAS-qualified and not a default.
    merkle_db: /path/to/merkle.db      # optional; defaults to $NOVAFABRIC_HOME/novaseal-merkle.db
                                       # (or a postgres:// DSN; NOVAFABRIC_SEAL_DB_PATH overrides)

novaseal.yaml schema (aws_kms profile):

    profile: aws_kms
    kms_key_id: arn:aws:kms:us-east-1:123456789012:key/mrk-...
    aws_region: us-east-1              # optional; default us-east-1
    cert_path: /path/to/cert.pem       # cert exported for the KMS key
    tsa_url: https://tsa.example.org/tsr  # optional (opt-in)
    merkle_db: /path/to/merkle.db      # optional

novaseal.yaml schema (azure_kv profile):

    profile: azure_kv
    vault_url: https://myvault.vault.azure.net/
    key_name: my-ec-key
    cert_path: /path/to/cert.pem
    tsa_url: https://tsa.example.org/tsr  # optional (opt-in)
    merkle_db: /path/to/merkle.db      # optional

novaseal.yaml schema (gcp_kms profile):

    profile: gcp_kms
    key_version_name: projects/P/locations/L/keyRings/R/cryptoKeys/K/cryptoKeyVersions/1
    cert_path: /path/to/cert.pem
    tsa_url: https://tsa.example.org/tsr  # optional (opt-in)
    merkle_db: /path/to/merkle.db      # optional

All four profiles also accept the optional ``tsa_urls`` ordered fallback list
shown in the local-profile example above (REG-ADR-007); it works identically
regardless of signing profile since it only affects the timestamp request.

All four profiles also accept an optional ``ca_bundle`` (ADR-0055, experimental):

    ca_bundle: /etc/novaseal/ca-bundle.crt   # operator CA chain (PEM, concatenated)

When set, ``nova verify`` additionally validates the DSSE signer certificate's
chain against it, offline. Omit it to keep verification unchanged.

With a ``ca_bundle``, the optional ``crl_dir`` / ``crl_strict`` keys (ADR-0070 §3,
experimental) add an offline CRL revocation check of the validated path:

    crl_dir: /var/lib/novaseal/crl     # CRLs (DER/PEM) synced by an operator cron job
    crl_strict: false                  # true: missing/stale/invalid CRL also fails

The optional ``tsa_ca_certs`` key (ADR-0070 §1/§5, experimental) lists PEM files of
operator-trusted TSA CA certificates. When set, ``nova verify`` also verifies the
RFC 3161 token's CMS signature, the TSA certificate's critical id-kp-timeStamping
EKU and its chain to these anchors at the token's genTime, offline. ``crl_dir`` /
``crl_strict`` then apply to the TSA chain as well:

    tsa_ca_certs:
      - /etc/novaseal/tsa/freetsa-cacert.pem

Without ``tsa_ca_certs`` (or ``nova verify --tsa-ca-bundle``) a timestamp is reported
``timestamp_trust: unanchored``: the token is intact, but its time is not trusted.
There is no built-in default TSA trust bundle, and ``tsa_url`` anchors nothing (it
is only where seals request tokens).

The optional ``previous_merkle_dbs`` key (ADR-0297 Amendment 2) lists this host's
earlier Merkle logs — after an ADR-0313 migration to a new v2 ``merkle_db``, the
retired v1 log. Verification only; nothing is appended to them. Without it, a v1
capsule verified on a host whose log is v2 reports ``sealer_log:
unverifiable-other-version`` (``nova verify --strict`` fails on it):

    merkle_db: ~/.novafabric/novaseal-merkle-v2.db
    previous_merkle_dbs:
      - ~/.novafabric/novaseal-merkle.db
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from novafabric._paths import nova_home

if TYPE_CHECKING:
    from novafabric.trust.novaseal.signing_backend import SigningBackend

try:
    import yaml  # already a runtime dep via capture/orchestrator
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

# ADR-0292: no default TSA. Until v0.102.x an omitted tsa_url meant
# "https://freetsa.org/tsr" — an outbound call to a third party on every sealed
# capture, from a local-mode core path. Empty means "do not timestamp".
_DEFAULT_TSA_URL = ""


def _default_merkle_db() -> Path:
    """``$NOVAFABRIC_HOME/novaseal-merkle.db`` — resolved per call, not at import."""
    return nova_home() / "novaseal-merkle.db"


def _default_config_path() -> Path:
    """``$NOVAFABRIC_HOME/novaseal.yaml`` — resolved per call, not at import."""
    return nova_home() / "novaseal.yaml"

_SUPPORTED_PROFILES = frozenset({"local", "aws_kms", "azure_kv", "gcp_kms"})
_MAX_TSA_CA_CERTS = 32
_MAX_PREVIOUS_MERKLE_DBS = 32


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
    # REG-ADR-007: ordered TSA fallback list — defaults to [tsa_url] when
    # novaseal.yaml doesn't set tsa_urls explicitly, and is empty when no TSA is
    # configured (ADR-0292: timestamping is opt-in). Never None.
    tsa_urls: list[str] = field(default_factory=list)
    # The resolved log location (see ``resolve_merkle_db_uri``): a ``Path`` for a
    # SQLite file, a ``str`` for a Postgres DSN — never a DSN wrapped in ``Path``.
    merkle_db: Path | str = field(default_factory=_default_merkle_db)
    # ADR-0055 (experimental): operator CA bundle for signer chain validation at
    # verify time. None = no chain validation (unchanged behaviour).
    ca_bundle: Optional[Path] = None
    # ADR-0070 §3 (experimental): directory of operator-synced CRLs checked against
    # the validated chain at verify time. None = no revocation check.
    crl_dir: Optional[Path] = None
    # crl_strict: a missing / stale / invalid CRL fails (default: visible warning).
    crl_strict: bool = False
    # ADR-0070 §5 (experimental): PEM files of operator-trusted TSA CA certificates
    # for RFC 3161 token trust-chain verification. Empty = no TSA chain check.
    tsa_ca_certs: list[Path] = field(default_factory=list)
    # ADR-0297 Amendment 2: this host's earlier Merkle logs (SQLite paths or
    # Postgres DSNs), e.g. the v1 log retired by an ADR-0313 migration. Verify-only:
    # nothing is ever appended to them. Empty = only ``merkle_db`` is consulted.
    previous_merkle_dbs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.tsa_urls:
            self.tsa_urls = [self.tsa_url] if self.tsa_url else []
        elif not self.tsa_url:
            self.tsa_url = self.tsa_urls[0]


class SealConfigError(Exception):
    """Raised when novaseal.yaml is missing or malformed."""


def _merkle_location(configured: object) -> Path | str:
    """The Merkle log location for a ``merkle_db`` value (``None``: not set).

    The single precedence rule every NovaSeal caller goes through (audit M2):

    1. ``NOVAFABRIC_SEAL_DB_PATH`` — a process-wide override (CI, tests, a job
       that must seal into a specific log);
    2. ``merkle_db`` in the ``novaseal.yaml`` being read;
    3. ``$NOVAFABRIC_HOME/novaseal-merkle.db``.

    A Postgres DSN stays a string (audit H2). Raises :class:`SealConfigError`
    for a location that is neither a path nor a supported DSN; the message
    never carries a password.
    """
    from novafabric.trust.novaseal.log_location import (  # noqa: PLC0415
        MerkleLocationError,
        normalize_location,
    )

    env_db = os.environ.get("NOVAFABRIC_SEAL_DB_PATH")
    source = "NOVAFABRIC_SEAL_DB_PATH"
    value: Path | str
    if env_db:
        value = env_db
    elif configured is not None and str(configured).strip():
        value, source = str(configured), "novaseal.yaml merkle_db"
    else:
        return _default_merkle_db()
    try:
        return normalize_location(value)
    except MerkleLocationError as exc:
        raise SealConfigError(f"{source}: {exc}") from exc


def resolve_merkle_db_uri(config_path: Path | None = None) -> str:
    """Canonical Merkle log location — a file path or a Postgres DSN, as a string.

    Precedence (``_merkle_location``): ``NOVAFABRIC_SEAL_DB_PATH``, then
    ``merkle_db`` in *config_path* (default: the discovered ``novaseal.yaml`` —
    ``NOVAFABRIC_SEAL_CONFIG``, else ``$NOVAFABRIC_HOME/novaseal.yaml``), then
    ``$NOVAFABRIC_HOME/novaseal-merkle.db``. ``load_signing_profile().merkle_db``
    is computed by the same rule, so the log capture seals into is the log every
    verifier, the SealTab and ``nova seal log verify`` read.

    A ``novaseal.yaml`` that is not valid YAML is skipped (the default applies);
    a ``merkle_db`` that is neither a path nor a supported DSN raises
    :class:`SealConfigError`.
    """
    configured: object = None
    if not os.environ.get("NOVAFABRIC_SEAL_DB_PATH"):
        if config_path is None:
            env_cfg = os.environ.get("NOVAFABRIC_SEAL_CONFIG")
            config_path = Path(env_cfg) if env_cfg else _default_config_path()
        configured = _configured_merkle_db(config_path)
    return str(_merkle_location(configured))


def _configured_merkle_db(config_path: Path) -> object:
    """The raw ``merkle_db`` of *config_path*, or None (absent, unreadable, not YAML)."""
    if yaml is None or not config_path.is_file():
        return None
    try:
        with open(config_path) as f:
            raw = yaml.safe_load(f)
    except Exception:  # noqa: BLE001 — malformed yaml: fall through to the default
        return None
    return raw.get("merkle_db") if isinstance(raw, dict) else None


def resolve_merkle_db_path() -> Path:
    """``resolve_merkle_db_uri()`` for callers that need a SQLite file.

    Raises ``ValueError`` when the log is a Postgres DSN (the message shows the
    DSN with its password redacted).
    """
    from novafabric.trust.novaseal.log_location import (  # noqa: PLC0415
        is_postgres_dsn,
        redact_location,
    )

    uri = resolve_merkle_db_uri()
    if is_postgres_dsn(uri):
        raise ValueError(
            f"the NovaSeal Merkle log is a Postgres DSN ({redact_location(uri)}); "
            "this operation needs a SQLite file — use resolve_merkle_db_uri() and "
            "open_merkle_log() instead."
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

    default_config = _default_config_path()
    if default_config.exists():
        return _parse_profile(default_config)

    return None


def _parse_profile(path: Path) -> SigningProfile:
    if yaml is None:  # pragma: no cover
        raise SealConfigError("PyYAML is required for novaseal config parsing")
    with open(path) as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise SealConfigError(f"novaseal.yaml must be a YAML mapping: {path}")
    previous_merkle_dbs = _parse_previous_merkle_dbs(raw)
    return replace(_profile_from_raw(raw), previous_merkle_dbs=previous_merkle_dbs)


def _profile_from_raw(raw: dict[str, Any]) -> SigningProfile:
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

    # YAML ``tsa_url:`` with no value is None — it must mean "no TSA", never the
    # literal string "None" (which was then used as a URL until v0.102.x).
    raw_tsa_url = raw.get("tsa_url")
    tsa_url = "" if raw_tsa_url is None else str(raw_tsa_url).strip()
    # Never mkdir here: loading a profile has no side effects, and a DSN is not
    # a path (audit H2). MerkleLog creates a SQLite file's directory on open.
    merkle_db = _merkle_location(raw.get("merkle_db"))

    # REG-ADR-007: optional ordered TSA fallback list. tsa_urls[0] need not
    # equal tsa_url — an operator may want a distinct primary vs. fallback
    # set — but if tsa_urls is omitted entirely, SigningProfile.__post_init__
    # defaults it to [tsa_url].
    raw_tsa_urls = raw.get("tsa_urls")
    if raw_tsa_urls is not None:
        if not isinstance(raw_tsa_urls, list) or not all(
            isinstance(u, str) for u in raw_tsa_urls
        ):
            raise SealConfigError("novaseal.yaml tsa_urls must be a list of strings")
        if not raw_tsa_urls:
            raise SealConfigError("novaseal.yaml tsa_urls, if given, must not be empty")
    if raw_tsa_urls is not None:
        tsa_urls = list(raw_tsa_urls)
        tsa_url = tsa_url or tsa_urls[0]  # tsa_urls alone is an explicit opt-in
    else:
        tsa_urls = [tsa_url] if tsa_url else []
    ca_bundle = _parse_ca_bundle(raw)
    crl_dir, crl_strict = _parse_crl_settings(raw)
    tsa_ca_certs = _parse_tsa_ca_certs(raw)
    if (crl_dir is not None or crl_strict) and ca_bundle is None and not tsa_ca_certs:
        # Revocation is only checked on a validated chain (the signer's CA-bundle
        # chain or the TSA chain); accepting the keys without either would
        # silently check nothing (ADR-0070 §3).
        raise SealConfigError(
            "novaseal.yaml crl_dir / crl_strict require ca_bundle or tsa_ca_certs: "
            "revocation is checked only on a validated certificate chain"
        )

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
            tsa_urls=tsa_urls,
            merkle_db=merkle_db,
            ca_bundle=ca_bundle,
            crl_dir=crl_dir,
            crl_strict=crl_strict,
            tsa_ca_certs=tsa_ca_certs,
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
            tsa_urls=tsa_urls,
            merkle_db=merkle_db,
            ca_bundle=ca_bundle,
            crl_dir=crl_dir,
            crl_strict=crl_strict,
            tsa_ca_certs=tsa_ca_certs,
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
            tsa_urls=tsa_urls,
            merkle_db=merkle_db,
            ca_bundle=ca_bundle,
            crl_dir=crl_dir,
            crl_strict=crl_strict,
            tsa_ca_certs=tsa_ca_certs,
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
        tsa_urls=tsa_urls,
        merkle_db=merkle_db,
        ca_bundle=ca_bundle,
        crl_dir=crl_dir,
        crl_strict=crl_strict,
        tsa_ca_certs=tsa_ca_certs,
    )


def _parse_previous_merkle_dbs(raw: dict[str, object]) -> list[str]:
    """Parse the optional ``previous_merkle_dbs`` list (ADR-0297 Amendment 2).

    This host's earlier Merkle logs — after an ADR-0313 migration, the v1 log the
    host sealed into before it started a v2 one. ``nova verify`` checks a capsule
    against them as well, so a v1-era capsule is confirmed by the log that sealed
    it instead of reporting ``sealer_log: unverifiable-other-version``. Entries are
    SQLite paths or Postgres DSNs. A listed SQLite file that does not exist is a
    hard config error, never a silent downgrade to "not checked". Bounded to 32.
    """
    value = raw.get("previous_merkle_dbs")
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        raise SealConfigError(
            "novaseal.yaml previous_merkle_dbs must be a list of non-empty path/DSN strings"
        )
    if len(value) > _MAX_PREVIOUS_MERKLE_DBS:
        raise SealConfigError(
            f"novaseal.yaml previous_merkle_dbs lists {len(value)} logs; at most "
            f"{_MAX_PREVIOUS_MERKLE_DBS} are allowed"
        )
    from novafabric.trust.novaseal.log_location import (  # noqa: PLC0415
        MerkleLocationError,
        normalize_location,
    )

    locations: list[str] = []
    for entry in value:
        try:
            location = normalize_location(entry)
        except MerkleLocationError as exc:
            raise SealConfigError(f"novaseal.yaml previous_merkle_dbs: {exc}") from exc
        if isinstance(location, Path) and not location.is_file():
            raise SealConfigError(f"NovaSeal previous_merkle_dbs entry not found: {location}")
        locations.append(str(location))
    return locations


def _parse_ca_bundle(raw: dict[str, object]) -> Optional[Path]:
    """Parse the optional ``ca_bundle`` key (ADR-0055); a set-but-missing path fails.

    A configured-but-absent bundle is a hard config error rather than a silent
    downgrade to "no chain validation".
    """
    value = raw.get("ca_bundle")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SealConfigError("novaseal.yaml ca_bundle must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_file():
        raise SealConfigError(f"NovaSeal ca_bundle not found: {path}")
    return path


def _parse_crl_settings(raw: dict[str, object]) -> tuple[Optional[Path], bool]:
    """Parse the optional ``crl_dir`` / ``crl_strict`` keys (ADR-0070 §3).

    A configured-but-absent directory is a hard config error, never a silent
    downgrade to "no revocation check".
    """
    strict = raw.get("crl_strict", False)
    if not isinstance(strict, bool):
        raise SealConfigError("novaseal.yaml crl_strict must be true or false")
    value = raw.get("crl_dir")
    if value is None:
        return None, strict
    if not isinstance(value, str) or not value.strip():
        raise SealConfigError("novaseal.yaml crl_dir must be a non-empty path string")
    path = Path(value).expanduser()
    if not path.is_dir():
        raise SealConfigError(f"NovaSeal crl_dir not found or not a directory: {path}")
    return path, strict


def _parse_tsa_ca_certs(raw: dict[str, object]) -> list[Path]:
    """Parse the optional ``tsa_ca_certs`` list of PEM paths (ADR-0070 §5).

    Every listed file must exist: a configured-but-absent anchor is a hard config
    error, never a silent downgrade to "no TSA chain check". Bounded to 32 entries.
    """
    value = raw.get("tsa_ca_certs")
    if value is None:
        return []
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and v.strip() for v in value)
    ):
        raise SealConfigError(
            "novaseal.yaml tsa_ca_certs must be a non-empty list of path strings"
        )
    if len(value) > _MAX_TSA_CA_CERTS:
        raise SealConfigError(
            f"novaseal.yaml tsa_ca_certs lists more than {_MAX_TSA_CA_CERTS} files"
        )
    paths = [Path(v).expanduser() for v in value]
    for path in paths:
        if not path.is_file():
            raise SealConfigError(f"NovaSeal tsa_ca_certs file not found: {path}")
    return paths


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
