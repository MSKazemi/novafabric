"""Pack 0.7.0 — AWS and GitHub credentials (ADR-0009 names the gitleaks rule set).

Until 0.7.0 the pack carried no rule for either, so an AWS key pair or a GitHub
token printed by a workload stayed verbatim in the capsule. The new rules keep the
two invariants the pack already had:

* only rules with a distinctive anchor may DROP a binary (a prefix-less rule would
  delete ordinary binaries -- see ``test_redaction_full_coverage``);
* ``match_hash`` hashes the secret value alone, so the ADR-0009 "hash a candidate,
  compare" primitive keeps working for the context-anchored AWS secret rule.

Fixture tokens are assembled at runtime so the SOURCE never contains a contiguous
provider-shaped token: GitHub push protection scans the public mirror's bytes.
"""

from __future__ import annotations

import hashlib
import json
import random
import string
from pathlib import Path

import jsonschema
import pytest

from novafabric.capture import secrets as secrets_mod
from novafabric.capture.secrets import (
    _BINARY_EXCLUDED_RULES,
    _DROP_RULES,
    _RULES,
    PACK_VERSION,
    SecretScannerV0,
    redact_secrets_in_text,
    scan_text_rule_ids,
)

SCHEMA = json.loads(
    (
        Path(__file__).parents[2] / "src/novafabric/schemas/secret-redaction.schema.json"
    ).read_text()
)
RUN_ID = "01HXAY7M5JZ8R7K4P9DPBYK2WX"

AWS_KEY_ID = "AKIA" + "QZ7XK2M4PZT3W6RN"
AWS_STS_KEY_ID = "ASIA" + "QZ7XK2M4PZT3W6RN"
AWS_SECRET = ("Ab3dEf6hIj/+" * 4)[:40]
GITHUB_CLASSIC = "ghp" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp"
GITHUB_FINE = "github" + "_pat_" + "11" + "A" * 20 + "_" + "Ab3dEf6hIj" * 5 + "Ab3dEf6hI"

_BY_ID = {str(r["id"]): r for r in _RULES}


def test_fixture_shapes() -> None:
    assert len(AWS_KEY_ID) == 20 and len(AWS_SECRET) == 40
    assert len(GITHUB_CLASSIC) == 40


def test_pack_version_and_new_rule_ids() -> None:
    assert PACK_VERSION == "0.7.0"
    assert {
        "aws-access-key-id",
        "aws-secret-access-key",
        "github-token",
        "github-fine-grained-pat",
    } <= set(_BY_ID)


@pytest.mark.parametrize(
    ("secret", "rule_id"),
    [
        (AWS_KEY_ID, "aws-access-key-id"),
        (AWS_STS_KEY_ID, "aws-access-key-id"),
        (GITHUB_CLASSIC, "github-token"),
        ("gho" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp", "github-token"),
        ("ghs" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp", "github-token"),
        ("ghu" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp", "github-token"),
        ("ghr" + "_" + "Ab3dEf6hIj" * 3 + "Kl3mNp", "github-token"),
        (GITHUB_FINE, "github-fine-grained-pat"),
    ],
)
def test_prefixed_credentials_are_redacted_in_full(secret: str, rule_id: str) -> None:
    out = redact_secrets_in_text(f"value: {secret} tail")
    assert out == f"value: [REDACTED:{rule_id}] tail"
    assert rule_id in scan_text_rule_ids(secret)


@pytest.mark.parametrize(
    "template",
    [
        "AWS_SECRET_ACCESS_KEY={v}",
        "export AWS_SECRET_ACCESS_KEY=\"{v}\"",
        "aws_secret_access_key = {v}",  # ~/.aws/credentials (ini)
        "aws_secret_access_key: '{v}'",  # YAML
        '{{"aws_secret_access_key": "{v}"}}',  # JSON
        '{{"msg": "{{\\"AWS_SECRET_ACCESS_KEY\\": \\"{v}\\"}}"}}',  # JSON inside a JSONL string
        '"SecretAccessKey": "{v}"',  # `aws sts assume-role` output
        "boto3.client('s3', aws_secret_access_key='{v}')",
        "aws configure set --aws-secret-access-key {v}",
    ],
)
def test_aws_secret_key_is_redacted_when_its_key_name_anchors_it(template: str) -> None:
    line = template.format(v=AWS_SECRET)
    out = redact_secrets_in_text(line)
    assert AWS_SECRET not in out
    assert "[REDACTED:aws-secret-access-key]" in out
    # only the value is replaced -- the key name stays, so the line is still legible
    assert out == line.replace(AWS_SECRET, "[REDACTED:aws-secret-access-key]")


@pytest.mark.parametrize(
    "benign",
    [
        AWS_SECRET,  # a bare 40-char base64 run is NOT an AWS secret on its own
        "sha1 8f14e45fceea167a5a36dedd4bea2543deadbeef",
        "secret_access_key_id=" + "x" * 12,  # too short to be a value
        "XAKIA" + "QZ7XK2M4PZT3W6RN",  # embedded in a longer token
        "akia" + "qz7xk2m4pzt3w6rn",  # lowercase is not an access key id
        "ghp" + "_" + "short",  # under the 36-char body
        "sha256:" + "a" * 64,
    ],
)
def test_new_rules_do_not_fire_on_benign_tokens(benign: str) -> None:
    hits = set(scan_text_rule_ids(benign)) & {
        "aws-access-key-id", "aws-secret-access-key", "github-token", "github-fine-grained-pat"
    }
    assert not hits, f"{hits} fired on {benign!r}"


def test_anchored_rule_match_hash_covers_the_value_only(tmp_path: Path) -> None:
    """ADR-0009: sha256(candidate) == match_hash must hold for the secret itself."""
    line = f"AWS_SECRET_ACCESS_KEY={AWS_SECRET}\n"
    (tmp_path / "env.lock").write_text(line)

    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()

    jsonschema.validate(proof, SCHEMA, format_checker=jsonschema.FormatChecker())
    (finding,) = [f for f in proof["findings"] if f["rule_id"] == "aws-secret-access-key"]
    assert finding["match_hash"] == "sha256:" + hashlib.sha256(AWS_SECRET.encode()).hexdigest()
    assert finding["byte_offset"] == line.index(AWS_SECRET)
    assert finding["byte_length"] == 40
    assert (tmp_path / "env.lock").read_text() == (
        "AWS_SECRET_ACCESS_KEY=[REDACTED:aws-secret-access-key]\n"
    )


@pytest.mark.parametrize("secret", [AWS_KEY_ID, GITHUB_CLASSIC, GITHUB_FINE])
def test_binary_carrying_a_new_credential_is_dropped(tmp_path: Path, secret: str) -> None:
    out = tmp_path / "outputs" / "core.bin"
    out.parent.mkdir()
    out.write_bytes(b"\x00\xff" + secret.encode() + b"\x00")

    proof = SecretScannerV0(capsule_dir=tmp_path, run_id=RUN_ID).scan_and_redact()

    assert not out.exists()
    assert proof["findings"] and all(f["redaction_strategy"] == "drop" for f in proof["findings"])


def test_drop_eligible_rules_are_exactly_the_anchored_ones() -> None:
    """Adding a rule forces a decision on whether it may delete a binary."""
    assert _BINARY_EXCLUDED_RULES == {"cohere-api-key", "together-api-key", "mistral-api-key"}
    assert {str(r["id"]) for r in _DROP_RULES} == set(_BY_ID) - _BINARY_EXCLUDED_RULES


def test_drop_eligible_rules_never_fire_on_random_binary_noise() -> None:
    """The invariant behind 'only prefixed rules may drop': unanchored content -- random
    bytes plus long alphanumeric, base64 and hex runs -- never trips a drop rule."""
    rng = random.Random(20261008)
    alphabets = [string.ascii_letters + string.digits, string.hexdigits, string.ascii_uppercase
                 + "234567", string.ascii_letters + string.digits + "/+"]
    chunks: list[bytes] = []
    for _ in range(4000):
        chunks.append(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 64))))
        alpha = rng.choice(alphabets)
        chunks.append("".join(rng.choice(alpha) for _ in range(rng.randrange(8, 120))).encode())
    blob = b"".join(chunks)
    for run in secrets_mod._BINARY_STRING_RE.finditer(blob):
        text = run.group().decode("ascii")
        for rule in _DROP_RULES:
            assert rule["pattern"].search(text) is None, (rule["id"], text)
