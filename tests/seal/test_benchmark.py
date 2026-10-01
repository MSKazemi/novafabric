"""NovaSeal.seal() latency benchmark — median CI gate + p99 tail alarm.

Benchmark: 100 seal() rounds, local ECDSA P-256 key, no TSA.
CI gate:   the median must stay below ``seal.seal-call.median`` (50 ms).
Tail alarm: a p99 above ``seal.seal-call.p99`` (200 ms) is reported as a
           warning (pytest warning + GitHub ``::warning::`` annotation +
           ``extra_info`` in the benchmark JSON) — it does NOT fail the job.

Why not gate on p99 (BL-061): across 55 ``seal-latency-gate`` runs on shared
GitHub runners (2026-09-08 → 2026-09-29) the per-run median ranged
0.53–9.2 ms (fleet median 0.82 ms) while the per-run max reached 262 ms, with
3/55 runs carrying a >200 ms sample and 8/55 a >100 ms one. With 100 rounds the
nearest-rank p99 is the second-slowest sample, so two scheduler stalls fail
the gate regardless of the code under test. The median is insensitive to a few
stalls and still catches a systematic regression; 50 ms is 5.4x the worst
observed per-run median.

Normal unit-test run (--benchmark-disable):
    The gate is skipped because there is only one timing sample;
    the benchmark still calls seal() once to verify correctness.

Dedicated CI step (no --benchmark-disable):
    uv run pytest tests/seal/test_benchmark.py -v \\
        --benchmark-json=bench-results/seal_latency.json
"""
from __future__ import annotations

import datetime
import math
import os
import statistics
import warnings

import pytest
from bench.slo import slo_value
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
# Both numbers live in the SLO catalog (ADR-0248) so the published claim and the
# gate cannot disagree.
_MEDIAN_LIMIT_S = slo_value("seal.seal-call.median")
_P99_ALARM_S = slo_value("seal.seal-call.p99")
_MIN_SAMPLES = 10


class SealTailLatencyAlarm(UserWarning):
    """The p99 crossed its alarm threshold; investigate, but the build stays green."""


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


def test_seal_latency_gate(benchmark: pytest.FixtureRequest, bench_seal: NovaSeal) -> None:
    """NovaSeal.seal() median must be below 50 ms; p99 > 200 ms raises an alarm.

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
    if len(raw) < _MIN_SAMPLES:
        pytest.skip("Too few samples — run without --benchmark-disable to enforce the gate")

    verdict = evaluate_latency(raw, median_limit_s=_MEDIAN_LIMIT_S, p99_alarm_s=_P99_ALARM_S)
    benchmark.extra_info.update(verdict.as_extra_info())  # type: ignore[attr-defined]
    if verdict.tail_alarm:
        _raise_tail_alarm(verdict)
    assert verdict.median_s < _MEDIAN_LIMIT_S, (
        f"seal() median={verdict.median_s * 1000:.2f}ms exceeds "
        f"{_MEDIAN_LIMIT_S * 1000:.0f}ms CI gate ({verdict.summary()})"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _p99(data: list[float]) -> float:
    """Nearest-rank 99th percentile without external dependencies."""
    s = sorted(data)
    idx = min(math.ceil(0.99 * len(s)) - 1, len(s) - 1)
    return s[idx]


class LatencyVerdict:
    """Median gate input plus the tail-alarm decision for one benchmark run."""

    __slots__ = ("max_s", "median_s", "min_s", "n", "p99_alarm_s", "p99_s")

    def __init__(self, data: list[float], p99_alarm_s: float) -> None:
        if not data:
            raise ValueError("LatencyVerdict needs at least one sample")
        self.n = len(data)
        self.min_s = min(data)
        self.max_s = max(data)
        self.median_s = statistics.median(data)
        self.p99_s = _p99(data)
        self.p99_alarm_s = p99_alarm_s

    @property
    def tail_alarm(self) -> bool:
        return self.p99_s >= self.p99_alarm_s

    def summary(self) -> str:
        return (
            f"n={self.n}, min={self.min_s * 1000:.2f}ms, median={self.median_s * 1000:.2f}ms, "
            f"p99={self.p99_s * 1000:.2f}ms, max={self.max_s * 1000:.2f}ms"
        )

    def as_extra_info(self) -> dict[str, float | bool]:
        return {
            "median_s": self.median_s,
            "p99_s": self.p99_s,
            "p99_alarm_s": self.p99_alarm_s,
            "tail_alarm": self.tail_alarm,
        }


def evaluate_latency(
    data: list[float], *, median_limit_s: float, p99_alarm_s: float
) -> LatencyVerdict:
    """Compute the verdict; ``median_limit_s`` is validated, the caller asserts it."""
    if median_limit_s <= 0 or p99_alarm_s <= 0:
        raise ValueError("latency thresholds must be positive")
    return LatencyVerdict(data, p99_alarm_s)


def _raise_tail_alarm(verdict: LatencyVerdict) -> None:
    message = (
        f"seal() p99={verdict.p99_s * 1000:.1f}ms is above the "
        f"{verdict.p99_alarm_s * 1000:.0f}ms tail alarm ({verdict.summary()}) — "
        "non-blocking; a repeat on consecutive runs is a real regression"
    )
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=NovaSeal tail latency alarm::{message}")  # noqa: T201
    warnings.warn(message, SealTailLatencyAlarm, stacklevel=2)


# ---------------------------------------------------------------------------
# Verdict logic (runs in the unit tier, no timing involved)
# ---------------------------------------------------------------------------

_MS = 0.001


def test_two_scheduler_stalls_raise_the_alarm_but_not_the_gate() -> None:
    """The BL-061 failure shape: a sub-ms median with two >200 ms stalls."""
    data = [0.8 * _MS] * 98 + [210 * _MS, 262 * _MS]
    verdict = evaluate_latency(data, median_limit_s=_MEDIAN_LIMIT_S, p99_alarm_s=_P99_ALARM_S)
    assert verdict.tail_alarm
    assert verdict.median_s < _MEDIAN_LIMIT_S


def test_systematic_regression_fails_the_median_gate() -> None:
    data = [60 * _MS] * 100
    verdict = evaluate_latency(data, median_limit_s=_MEDIAN_LIMIT_S, p99_alarm_s=_P99_ALARM_S)
    assert verdict.median_s >= _MEDIAN_LIMIT_S
    assert not verdict.tail_alarm


def test_healthy_run_is_quiet() -> None:
    data = [(0.5 + i / 100) * _MS for i in range(100)]
    verdict = evaluate_latency(data, median_limit_s=_MEDIAN_LIMIT_S, p99_alarm_s=_P99_ALARM_S)
    assert not verdict.tail_alarm
    assert verdict.as_extra_info()["tail_alarm"] is False
    assert "n=100" in verdict.summary()


def test_tail_alarm_warns_and_annotates_on_github(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    verdict = evaluate_latency(
        [1 * _MS] * 98 + [300 * _MS] * 2, median_limit_s=_MEDIAN_LIMIT_S, p99_alarm_s=_P99_ALARM_S
    )
    with pytest.warns(SealTailLatencyAlarm, match="non-blocking"):
        _raise_tail_alarm(verdict)
    assert "::warning title=NovaSeal tail latency alarm::" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("data", "median", "p99"),
    [([], 0.05, 0.2), ([1.0], 0.0, 0.2), ([1.0], 0.05, -1.0)],
)
def test_invalid_inputs_raise(data: list[float], median: float, p99: float) -> None:
    with pytest.raises(ValueError):
        evaluate_latency(data, median_limit_s=median, p99_alarm_s=p99)


def test_gate_thresholds_come_from_the_catalog() -> None:
    assert _MEDIAN_LIMIT_S == pytest.approx(0.050)
    assert _P99_ALARM_S == pytest.approx(0.200)
    assert _MEDIAN_LIMIT_S < _P99_ALARM_S
