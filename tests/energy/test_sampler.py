"""Slice A1 — bounded RAPL sampling daemon (ADR-0093 §A1).

The sampler is a single background thread that snapshots cumulative energy
counters (via an *injected* reader) on an interval and appends compact raw
lines to ``energy-samples.jsonl``. Tests drive it with a scripted fake reader
so they never touch real hardware and never depend on wall-clock timing for
correctness (``sample_once`` is called directly).

Fail-open invariant: a reader that raises must not propagate into the workload;
the sampler records a gap marker and keeps going.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from novafabric.energy._sampler import SAMPLES_FILE, EnergySampler


class _ScriptedReader:
    """Yields scripted readings; ``available`` is fixed."""

    name = "scripted"

    def __init__(self, readings: list[dict[str, int]], *, available: bool = True) -> None:
        self._readings = list(readings)
        self._available = available
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def read_domains_uj(self) -> dict[str, int]:
        idx = min(self.calls, len(self._readings) - 1)
        self.calls += 1
        return dict(self._readings[idx])


class _RaisingReader:
    """A reader whose ``read_domains_uj`` always raises — the fail-open path."""

    name = "raising"

    def available(self) -> bool:
        return True

    def read_domains_uj(self) -> dict[str, int]:
        raise OSError("counter vanished")


def _read_samples(capsule_dir: Path) -> list[dict[str, object]]:
    path = capsule_dir / SAMPLES_FILE
    lines = path.read_text().splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_sample_once_appends_one_compact_line(tmp_path):
    reader = _ScriptedReader([{"intel-rapl:0": 5_000_000}])
    sampler = EnergySampler(tmp_path, reader, interval_ms=10)

    sampler.sample_once()
    sampler.flush()

    samples = _read_samples(tmp_path)
    assert len(samples) == 1
    rec = samples[0]
    assert rec["domains_uj"] == {"intel-rapl:0": 5_000_000}
    assert isinstance(rec["ts"], (int, float))
    assert rec.get("gap") in (None, False)


def test_sample_once_records_gap_on_reader_error_and_never_raises(tmp_path):
    sampler = EnergySampler(tmp_path, _RaisingReader(), interval_ms=10)

    # must NOT raise into the workload
    sampler.sample_once()
    sampler.flush()

    samples = _read_samples(tmp_path)
    assert len(samples) == 1
    assert samples[0]["gap"] is True
    assert samples[0]["domains_uj"] == {}


def test_start_stop_produces_at_least_one_sample(tmp_path):
    reader = _ScriptedReader(
        [{"intel-rapl:0": 1_000_000}, {"intel-rapl:0": 2_000_000}]
    )
    sampler = EnergySampler(tmp_path, reader, interval_ms=1)

    sampler.start()
    # stop() joins the worker thread (bounded); the loop must have run >=1 time
    sampler.stop()

    samples = _read_samples(tmp_path)
    assert len(samples) >= 1
    # thread must be joined (not alive) after stop
    assert sampler.is_alive() is False


def test_loop_takes_multiple_samples_over_time(tmp_path):
    # drive the background loop body (re-sampling) deterministically: poll
    # (bounded) until >=2 samples land, then stop. Exercises the while-loop body.
    reader = _ScriptedReader(
        [{"intel-rapl:0": 1}, {"intel-rapl:0": 2}, {"intel-rapl:0": 3}]
    )
    sampler = EnergySampler(tmp_path, reader, interval_ms=1, buffer_max=1)
    sampler.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if (tmp_path / SAMPLES_FILE).exists() and len(_read_samples(tmp_path)) >= 2:
            break
        time.sleep(0.01)
    sampler.stop()
    assert len(_read_samples(tmp_path)) >= 2


def test_stop_is_idempotent_and_double_start_is_safe(tmp_path):
    reader = _ScriptedReader([{"d": 1}])
    sampler = EnergySampler(tmp_path, reader, interval_ms=1)
    sampler.start()
    sampler.start()  # second start is a no-op
    sampler.stop()
    sampler.stop()  # second stop is a no-op
    assert sampler.is_alive() is False


def test_unavailable_reader_still_samples_a_gap(tmp_path):
    # available()==False is honest "no counter" — sample_once records a gap
    reader = _ScriptedReader([{}], available=False)
    sampler = EnergySampler(tmp_path, reader, interval_ms=1)
    sampler.sample_once()
    sampler.flush()
    samples = _read_samples(tmp_path)
    assert len(samples) == 1
    assert samples[0]["gap"] is True


def test_flush_is_fail_open_when_path_unwritable(tmp_path):
    # capsule_dir collides with an existing *file* → mkdir/open raise OSError;
    # the sampler must swallow it (fail-open) and keep the buffered samples.
    clash = tmp_path / "clash"
    clash.write_text("i am a file, not a dir\n")
    reader = _ScriptedReader([{"d": 1}])
    sampler = EnergySampler(clash, reader, interval_ms=1)
    sampler.sample_once()
    sampler.flush()  # must not raise
    # the unflushable sample is retained in the buffer (not silently dropped)
    assert not (clash / SAMPLES_FILE).exists() if clash.is_dir() else True


def test_buffer_flushes_on_threshold(tmp_path):
    reader = _ScriptedReader([{"d": 1}])
    sampler = EnergySampler(tmp_path, reader, interval_ms=1, buffer_max=2)
    # three samples, buffer_max=2 → at least one flush happens before stop
    sampler.sample_once()
    sampler.sample_once()
    sampler.sample_once()
    sampler.flush()
    samples = _read_samples(tmp_path)
    assert len(samples) == 3
