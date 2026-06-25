"""NovaSeal.seal() latency benchmark — p99 CI gate (ADR-0041).

Benchmark: 100 seal() rounds, local ECDSA P-256 key, no TSA.
CI gate:   p99 must stay below 200 ms.

Normal unit-test run (--benchmark-disable):
    The p99 assertion is skipped because there is only one timing sample;
    the benchmark still calls seal() once to verify correctness.

Dedicated CI step (no --benchmark-disable):
    uv run pytest tests/seal/test_benchmark.py -v \\
        --benchmark-json=bench-results/seal_latency.json
"""
from __future__ import annotations

import datetime
import math

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from novafabric.trust.novaseal import KeyConfig, NovaSeal

_MANIFEST_BASE: dict[str, object] = {
    "run_id": "bench-latency",
    "model": "gpt-4o",
    "status": "complete",
}
_P99_LIMIT_S = 0.200  # 200 ms


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bench_seal(tmp_path_factory: pytest.TempPathFactory) -> NovaSeal:
    """NovaSeal with a local ECDSA P-256 key and no TSA, shared across rounds."""
    tmp = tmp_path_factory.mktemp("bench_seal")

    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp / "bench.key"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "NovaSeal-Bench")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp / "bench.crt"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    config = KeyConfig(profile="local", key_path=str(key_path), cert_path=str(cert_path))
    return NovaSeal(config=config, tsa_url="", db_path=str(tmp / "bench.db"))


# ---------------------------------------------------------------------------
# Benchmark
# ---------------------------------------------------------------------------


def test_seal_p99_latency_gate(benchmark: pytest.FixtureRequest, bench_seal: NovaSeal) -> None:
    """NovaSeal.seal() p99 must be below 200 ms (ADR-0041 CI gate).

    Each round seals a distinct manifest (unique `seq` field) so Merkle-log
    append-path effects remain realistic and do not artificially flatten the
    latency distribution.

    The gate is skipped when --benchmark-disable is active (< 10 samples),
    which is the default for the regular unit-test step. It is enforced in
    the dedicated seal-latency-gate CI step.
    """
    counter = {"n": 0}

    def _seal() -> None:
        counter["n"] += 1
        bench_seal.seal({**_MANIFEST_BASE, "seq": counter["n"]})

    benchmark.pedantic(_seal, rounds=100, iterations=1, warmup_rounds=5)  # type: ignore[attr-defined]

    # benchmark.stats is None when --benchmark-disable is active; the fixture
    # still calls the function once for correctness but collects no timing data.
    # In pytest-benchmark 5.x, Metadata.stats is the inner Stats object with
    # the per-round timing list in its .data attribute.
    meta = benchmark.stats  # type: ignore[attr-defined]
    raw: list[float] = meta.stats.data if meta is not None else []
    if len(raw) < 10:
        pytest.skip("Too few samples — run without --benchmark-disable to enforce the p99 gate")

    p99 = _p99(raw)
    assert p99 < _P99_LIMIT_S, (
        f"seal() p99={p99 * 1000:.1f}ms exceeds {_P99_LIMIT_S * 1000:.0f}ms CI gate "
        f"(n={len(raw)}, "
        f"min={min(raw) * 1000:.1f}ms, "
        f"median={sorted(raw)[len(raw) // 2] * 1000:.1f}ms, "
        f"max={max(raw) * 1000:.1f}ms)"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _p99(data: list[float]) -> float:
    """Nearest-rank 99th percentile without external dependencies."""
    s = sorted(data)
    idx = min(math.ceil(0.99 * len(s)) - 1, len(s) - 1)
    return s[idx]
