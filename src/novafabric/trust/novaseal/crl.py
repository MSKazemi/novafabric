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
"""Offline CRL revocation checking for CA-bundle chains (ADR-0070 §3, ADR-0055 OQ-55-3).

*Experimental.* Air-gapped HPC nodes cannot reach a CRL distribution point at verify
time, so ADR-0070 §3 has an operator cron job sync CRLs into a local directory. This
module reads that directory — **it never fetches anything over the network** — and
checks every non-anchor certificate of an already-validated certification path
(leaf and intermediates) against the CRL published by that certificate's issuer.

Per-certificate outcome (:class:`CrlStatus`):

* ``good`` — an authentic, current CRL from the issuer does not list the serial.
* ``revoked`` — an authentic CRL from the issuer lists the serial with a revocation
  date at or before the validation time. Revocation is irreversible, so an authentic
  CRL counts even when stale (except ``certificateHold``, which only counts from the
  newest current CRL).
* ``no_crl`` — no CRL in the directory is issued by (and in scope for) the issuer.
* ``stale`` — only CRLs outside ``thisUpdate <= validation_time <= nextUpdate`` (or
  without ``nextUpdate``) are available.
* ``invalid_crl`` — CRLs name the issuer but none verifies under the issuer's public
  key (forged/unsigned), or the issuer certificate lacks the ``cRLSign`` key usage.

Policy (:func:`check_chain_revocation`): ``revoked`` always fails; by default the
other non-``good`` outcomes are **visible warnings** (soft-fail, ADR-0070 §3); with
``strict=True`` they fail too (``crl_strict``).

Bounded input: at most :data:`DEFAULT_MAX_CRL_FILES` candidate files (more is a
:class:`CrlStoreError` — fail closed rather than silently truncate) and
:data:`DEFAULT_MAX_CRL_BYTES` per file (larger files are skipped with a finding).
Hidden files (``.name``, e.g. a sync job's partial download) and non-regular files
are ignored. Files that are not CRLs, delta CRLs, indirect or partitioned CRLs, and
CRLs carrying an unrecognised critical extension are skipped with a finding.
"""

from __future__ import annotations

import datetime
import enum
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.x509 import Certificate, CertificateRevocationList

#: Upper bound on candidate CRL files read from the directory.
DEFAULT_MAX_CRL_FILES = 256
#: Upper bound on the size of a single CRL file (16 MiB covers large public-CA CRLs).
DEFAULT_MAX_CRL_BYTES = 16 * 1024 * 1024

_PEM_CRL_MARKER = b"-----BEGIN X509 CRL-----"
_KNOWN_CRITICAL_CRL_EXTENSIONS = frozenset(
    {x509.IssuingDistributionPoint.oid, x509.DeltaCRLIndicator.oid}
)


class CrlStoreError(ValueError):
    """Raised when the CRL directory itself is unusable (missing, too many files).

    Individual unusable files never raise; they become :class:`CrlFinding` entries.
    """


class CrlStatus(str, enum.Enum):
    """Revocation outcome for one certificate on the validated path."""

    GOOD = "good"
    REVOKED = "revoked"
    NO_CRL = "no_crl"
    STALE = "stale"
    INVALID_CRL = "invalid_crl"


@dataclass(frozen=True)
class CrlFinding:
    """A CRL-directory file that was skipped, and why."""

    source: str
    message: str


@dataclass(frozen=True)
class LoadedCrl:
    """A parsed CRL plus the file it came from."""

    source: str
    crl: CertificateRevocationList


@dataclass(frozen=True)
class CrlStore:
    """The usable CRLs of an operator directory plus findings for skipped files."""

    crls: tuple[LoadedCrl, ...]
    findings: tuple[CrlFinding, ...] = ()


@dataclass(frozen=True)
class CertRevocationStatus:
    """Revocation outcome for one certificate of the path.

    Attributes:
        subject: RFC 4514 subject of the checked certificate.
        serial: Serial number, lowercase hex.
        status: The :class:`CrlStatus`.
        detail: Human-readable explanation.
        crl_source: File name of the deciding CRL, when one was used.
        reason: CRLReason name (e.g. ``key_compromise``) when revoked and present.
        revocation_date: ISO-8601 revocation instant when revoked.
    """

    subject: str
    serial: str
    status: CrlStatus
    detail: str
    crl_source: str | None = None
    reason: str | None = None
    revocation_date: str | None = None


@dataclass(frozen=True)
class RevocationCheckResult:
    """Outcome of :func:`check_chain_revocation` for a whole validated path.

    Attributes:
        ok: ``False`` when any certificate is revoked, or (strict mode) when any
            certificate is not ``good``.
        strict: Whether strict (``crl_strict``) policy was applied.
        certificates: Per-certificate results, leaf first (trust anchor excluded).
        findings: Files skipped while loading the directory.
    """

    ok: bool
    strict: bool
    certificates: tuple[CertRevocationStatus, ...]
    findings: tuple[CrlFinding, ...] = ()

    @property
    def warnings(self) -> tuple[CertRevocationStatus, ...]:
        """Non-``good``, non-``revoked`` results (soft-fail warnings, or strict failures)."""
        return tuple(
            c for c in self.certificates if c.status not in (CrlStatus.GOOD, CrlStatus.REVOKED)
        )

    @property
    def summary(self) -> str:
        """One-line summary, e.g. ``"leaf: good; intermediate: no_crl"``."""
        return "; ".join(f"{c.subject}: {c.status.value}" for c in self.certificates) or (
            "no certificate below the trust anchor to check"
        )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _parse_crl(data: bytes) -> CertificateRevocationList:
    """Parse PEM or DER CRL bytes; raise ``ValueError`` when they are not a CRL."""
    if _PEM_CRL_MARKER in data:
        return x509.load_pem_x509_crl(data)
    return x509.load_der_x509_crl(data)


def _unsupported_reason(crl: CertificateRevocationList) -> str | None:
    """Why a parsed CRL cannot be used as a complete CRL, or ``None`` when usable."""
    for ext in crl.extensions:
        if ext.critical and ext.oid not in _KNOWN_CRITICAL_CRL_EXTENSIONS:
            return f"unsupported critical CRL extension {ext.oid.dotted_string}"
    try:
        crl.extensions.get_extension_for_class(x509.DeltaCRLIndicator)
        return "delta CRL ignored (only complete CRLs are supported)"
    except x509.ExtensionNotFound:
        pass
    idp = _idp(crl)
    if idp is not None:
        if idp.indirect_crl:
            return "indirect CRL ignored (not supported)"
        if idp.only_contains_attribute_certs:
            return "attribute-certificate CRL ignored"
        if idp.only_some_reasons:
            return "reason-partitioned CRL ignored (not supported)"
        if idp.relative_name is not None:
            return "CRL with a relative distribution-point name ignored (not supported)"
    return None


def load_crl_directory(
    directory: Path,
    *,
    max_files: int = DEFAULT_MAX_CRL_FILES,
    max_bytes: int = DEFAULT_MAX_CRL_BYTES,
) -> CrlStore:
    """Load every CRL (DER or PEM) from an operator directory, offline and bounded.

    Files are visited in sorted name order (deterministic). Hidden files and
    anything that is not a regular file are ignored.

    Args:
        directory: The CRL directory populated by the operator's sync job.
        max_files: Maximum candidate files; exceeding it raises (fail closed).
        max_bytes: Maximum bytes per file; larger files are skipped with a finding.

    Raises:
        CrlStoreError: The directory is missing/unreadable or holds too many files.
    """
    if max_files < 1 or max_bytes < 1:
        raise ValueError("max_files and max_bytes must be positive")
    try:
        entries = sorted(
            p for p in directory.iterdir() if not p.name.startswith(".") and p.is_file()
        )
    except OSError as exc:
        raise CrlStoreError(f"cannot read CRL directory {directory}: {exc}") from exc
    if len(entries) > max_files:
        raise CrlStoreError(
            f"CRL directory {directory} holds {len(entries)} files, over the limit of "
            f"{max_files}; refusing to check a truncated set"
        )
    crls: list[LoadedCrl] = []
    findings: list[CrlFinding] = []
    for path in entries:
        try:
            with path.open("rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as exc:
            findings.append(CrlFinding(path.name, f"unreadable: {exc}"))
            continue
        if len(data) > max_bytes:
            findings.append(CrlFinding(path.name, f"skipped: larger than {max_bytes} bytes"))
            continue
        try:
            crl = _parse_crl(data)
        except ValueError:
            findings.append(CrlFinding(path.name, "skipped: not a DER or PEM X.509 CRL"))
            continue
        unsupported = _unsupported_reason(crl)
        if unsupported is not None:
            findings.append(CrlFinding(path.name, unsupported))
            continue
        crls.append(LoadedCrl(source=path.name, crl=crl))
    return CrlStore(crls=tuple(crls), findings=tuple(findings))


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def _idp(crl: CertificateRevocationList) -> x509.IssuingDistributionPoint | None:
    try:
        return crl.extensions.get_extension_for_class(x509.IssuingDistributionPoint).value
    except x509.ExtensionNotFound:
        return None


def _is_ca(cert: Certificate) -> bool:
    try:
        return bool(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _cert_dp_names(cert: Certificate) -> set[x509.GeneralName]:
    try:
        cdp = cert.extensions.get_extension_for_class(x509.CRLDistributionPoints).value
    except x509.ExtensionNotFound:
        return set()
    return {name for dp in cdp for name in (dp.full_name or ())}


def _in_scope(crl: CertificateRevocationList, cert: Certificate) -> bool:
    """RFC 5280 §6.3.3 (b) scope check against the IssuingDistributionPoint."""
    idp = _idp(crl)
    if idp is None:
        return True
    if idp.only_contains_user_certs and _is_ca(cert):
        return False
    if idp.only_contains_ca_certs and not _is_ca(cert):
        return False
    if idp.full_name:
        return bool(set(idp.full_name) & _cert_dp_names(cert))
    return True


def _issuer_may_sign_crls(issuer: Certificate) -> bool:
    try:
        return bool(issuer.extensions.get_extension_for_class(x509.KeyUsage).value.crl_sign)
    except x509.ExtensionNotFound:
        return True


def _signature_valid(crl: CertificateRevocationList, issuer: Certificate) -> bool:
    try:
        return crl.is_signature_valid(issuer.public_key())  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _is_current(crl: CertificateRevocationList, when: datetime.datetime) -> bool:
    next_update = crl.next_update_utc
    return next_update is not None and crl.last_update_utc <= when <= next_update


def _entry_reason(entry: x509.RevokedCertificate) -> x509.ReasonFlags | None:
    try:
        return entry.extensions.get_extension_for_class(x509.CRLReason).value.reason
    except x509.ExtensionNotFound:
        return None


def _revoked_status(
    cert: Certificate,
    authentic: Sequence[LoadedCrl],
    newest_current: LoadedCrl | None,
    when: datetime.datetime,
    subject: str,
    serial: str,
) -> CertRevocationStatus | None:
    """Return a ``revoked`` status when an authentic CRL revokes ``cert`` by ``when``."""
    for loaded in authentic:
        entry = loaded.crl.get_revoked_certificate_by_serial_number(cert.serial_number)
        if entry is None or entry.revocation_date_utc > when:
            continue
        reason = _entry_reason(entry)
        if reason is x509.ReasonFlags.remove_from_crl:
            continue
        if reason is x509.ReasonFlags.certificate_hold and loaded is not newest_current:
            continue
        reason_name = reason.name if reason is not None else None
        return CertRevocationStatus(
            subject=subject,
            serial=serial,
            status=CrlStatus.REVOKED,
            detail=(
                f"revoked on {entry.revocation_date_utc.isoformat()}"
                + (f" ({reason_name})" if reason_name else "")
                + f" per {loaded.source}"
            ),
            crl_source=loaded.source,
            reason=reason_name,
            revocation_date=entry.revocation_date_utc.isoformat(),
        )
    return None


def check_certificate_revocation(
    cert: Certificate,
    issuer: Certificate,
    store: CrlStore,
    when: datetime.datetime,
) -> CertRevocationStatus:
    """Check one certificate against CRLs issued by ``issuer`` (never raises)."""
    subject = cert.subject.rfc4514_string()
    serial = format(cert.serial_number, "x")
    named = [c for c in store.crls if c.crl.issuer == issuer.subject]
    candidates = [c for c in named if _in_scope(c.crl, cert)]
    if not candidates:
        detail = f"no CRL from issuer {issuer.subject.rfc4514_string()} in the CRL directory"
        if named:
            detail += " covers this certificate (scope mismatch)"
        return CertRevocationStatus(subject, serial, CrlStatus.NO_CRL, detail)
    if not _issuer_may_sign_crls(issuer):
        return CertRevocationStatus(
            subject,
            serial,
            CrlStatus.INVALID_CRL,
            "issuer certificate's keyUsage does not permit cRLSign",
        )
    authentic = [c for c in candidates if _signature_valid(c.crl, issuer)]
    if not authentic:
        sources = ", ".join(c.source for c in candidates)
        return CertRevocationStatus(
            subject,
            serial,
            CrlStatus.INVALID_CRL,
            f"CRL signature does not verify under the issuer's public key ({sources})",
        )
    current = [c for c in authentic if _is_current(c.crl, when)]
    newest_current = max(current, key=lambda c: c.crl.last_update_utc) if current else None
    revoked = _revoked_status(cert, authentic, newest_current, when, subject, serial)
    if revoked is not None:
        return revoked
    if newest_current is not None:
        return CertRevocationStatus(
            subject,
            serial,
            CrlStatus.GOOD,
            f"not revoked per {newest_current.source} "
            f"(valid until {newest_current.crl.next_update_utc})",
            crl_source=newest_current.source,
        )
    freshest = max(authentic, key=lambda c: c.crl.last_update_utc)
    next_update = freshest.crl.next_update_utc
    window = f"thisUpdate {freshest.crl.last_update_utc.isoformat()}, nextUpdate " + (
        next_update.isoformat() if next_update is not None else "absent"
    )
    return CertRevocationStatus(
        subject,
        serial,
        CrlStatus.STALE,
        f"CRL {freshest.source} is not current at the validation time ({window})",
        crl_source=freshest.source,
    )


def check_chain_revocation(
    chain: Sequence[Certificate],
    store: CrlStore,
    *,
    validation_time: datetime.datetime | None = None,
    strict: bool = False,
) -> RevocationCheckResult:
    """Check every non-anchor certificate of a validated path against ``store``.

    Args:
        chain: The validated path, leaf first, trust anchor last (as returned by
            path validation). The anchor itself is not revocation-checked — remove
            it from the CA bundle to distrust it.
        store: CRLs from :func:`load_crl_directory`.
        validation_time: Instant to evaluate at; defaults to now (UTC). A naive
            datetime is interpreted as UTC.
        strict: ``crl_strict`` — also fail on ``no_crl`` / ``stale`` / ``invalid_crl``.

    Returns:
        A :class:`RevocationCheckResult`. ``revoked`` always makes ``ok`` false.
    """
    when = validation_time or datetime.datetime.now(datetime.timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    results = tuple(
        check_certificate_revocation(chain[i], chain[i + 1], store, when)
        for i in range(len(chain) - 1)
    )
    failing = {CrlStatus.REVOKED}
    if strict:
        failing |= {CrlStatus.NO_CRL, CrlStatus.STALE, CrlStatus.INVALID_CRL}
    ok = not any(r.status in failing for r in results)
    return RevocationCheckResult(
        ok=ok, strict=strict, certificates=results, findings=store.findings
    )
