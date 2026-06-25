#!/usr/bin/env python3
"""Generate ECDSA P-256 test keys for the promote subsystem tests.

Run once:
    python tests/fixtures/promote/generate_keys.py

Outputs (in the same directory):
    proposer.pem, proposer_cert.pem
    approver.pem, approver_cert.pem
    admin.pem, admin_cert.pem
"""

from __future__ import annotations

import datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

OUT_DIR = Path(__file__).parent / "keys"
OUT_DIR.mkdir(parents=True, exist_ok=True)

NAMES = ["proposer", "approver", "admin"]
VALID_YEARS = 10


def generate_key_and_cert(common_name: str) -> None:
    # Generate key
    private_key = ec.generate_private_key(ec.SECP256R1())

    # Build self-signed certificate
    subject = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=365 * VALID_YEARS))
        .sign(private_key, hashes.SHA256())
    )

    key_path = OUT_DIR / f"{common_name}.pem"
    cert_path = OUT_DIR / f"{common_name}_cert.pem"

    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(
        cert.public_bytes(serialization.Encoding.PEM)
    )
    print(f"Generated: {key_path.name}, {cert_path.name}")


if __name__ == "__main__":
    for name in NAMES:
        generate_key_and_cert(name)
    print("Done.")
