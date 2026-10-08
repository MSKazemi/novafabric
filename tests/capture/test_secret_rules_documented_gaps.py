"""The default pack's documented NON-detections are true -- and stay documented.

``docs/architecture/run-capsule.md`` ("What the scanner detects, and what it does
not") tells users which secret formats ``gitleaks-core-v0`` does NOT catch, so that
nobody reads a zero-finding ``redaction-proof.json`` as "this capsule holds no
secret" (issue novafabric-private#10). Each case below is one bullet of that list.

If a new rule starts matching one of these, the test fails on purpose: move the
format from the "Not detected" list to the "Detected" paragraph, then drop the case.

Fixture tokens are assembled at runtime so the SOURCE never contains a contiguous
provider-shaped token: GitHub push protection scans the public mirror's bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.capture.secrets import scan_text_rule_ids

DOC = Path(__file__).parents[2] / "docs/architecture/run-capsule.md"

UNDETECTED: dict[str, tuple[str, str]] = {
    # case id: (sample text, phrase the doc's "Not detected" list must carry)
    "aws-secret-bare-with-slash": (
        ("Ab3dEf6hIj/+" * 4)[:40],
        "bare 40-character AWS secret access key",
    ),
    "pinecone-legacy-uuid": ("3f2504e0-4f89-11d3-" + "9a0c-0305e82c3301", "bare UUID"),
    "pem-private-key": (
        "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIEow" + "IBAAKCAQEA" * 5,
        "PEM",
    ),
    "jwt": (
        "eyJhbGciOiJIUzI1NiJ9"
        + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        + ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
        "JWTs",
    ),
    "bearer-header": ("Authorization: Bearer " + "abcDEF123456ghiJKL789012mnoPQR", "Bearer"),
    "password-assignment": ("password=" + "hunter2Correct!", "password=…"),
    "db-url-credentials": (
        "postgres://admin:" + "S3cretPassw0rd" + "@db.example.com:5432/x",
        "connection string",
    ),
    "slack-bot-token": ("xox" + "b-123456789012-1234567890123-" + "AbCdEfGhIjKlMnOpQrStUvWx", "Slack"),
    "stripe-live-key": ("sk_" + "live_" + "4eC39HqLyjWDarjtT1zdp7dc", "Stripe"),
    "google-api-key": ("AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY", "Google"),
    "azure-openai-hex32": ("0123456789abcdef" * 2, "Azure"),
    "email-address": ("jane.doe" + "@example.com", "email addresses"),
}


@pytest.mark.parametrize("case", sorted(UNDETECTED))
def test_documented_gap_is_really_not_detected(case: str) -> None:
    sample, _ = UNDETECTED[case]
    assert scan_text_rule_ids(sample) == [], (
        f"{case} is now detected: move it from the 'Not detected' list in "
        f"{DOC.relative_to(DOC.parents[2])} to the 'Detected' paragraph"
    )


@pytest.mark.parametrize("case", sorted(UNDETECTED))
def test_documented_gap_is_named_in_the_doc(case: str) -> None:
    _, phrase = UNDETECTED[case]
    text = DOC.read_text(encoding="utf-8")
    start = text.index("**Not detected**")
    section = text[start : text.index("\n## ", start)]
    assert phrase in section, f"{case}: the 'Not detected' list no longer mentions {phrase!r}"


def test_doc_states_the_proof_is_not_a_proof_of_absence() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "It is **not** proof that\na capsule contains no secret" in text
