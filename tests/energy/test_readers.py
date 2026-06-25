"""Slice A1 — energy counter readers (ADR-0093).

RAPL via sysfs is stdlib-only (no dependency). These tests drive the reader
against a *fake* `/sys/class/powercap` tree so they run anywhere — the honest
degradation path (no counters) is the default and must never raise.
"""

from __future__ import annotations

from pathlib import Path

from novafabric.energy import CounterName
from novafabric.energy._readers import (
    NullReader,
    RaplSysfsReader,
    probe_counters,
    rapl_delta_joules,
)


def _make_rapl(tmp: Path, *, energy_uj: int, max_range_uj: int = 262143328850) -> Path:
    base = tmp / "powercap"
    dom = base / "intel-rapl:0"
    dom.mkdir(parents=True)
    (dom / "name").write_text("package-0\n")
    (dom / "energy_uj").write_text(f"{energy_uj}\n")
    (dom / "max_energy_range_uj").write_text(f"{max_range_uj}\n")
    return base


def test_null_reader_is_never_available_and_never_raises():
    r = NullReader()
    assert r.available() is False
    assert r.read_domains_uj() == {}


def test_rapl_reader_unavailable_when_tree_absent(tmp_path):
    r = RaplSysfsReader(base=tmp_path / "does-not-exist")
    assert r.available() is False
    # honest degradation: no raise, empty reading
    assert r.read_domains_uj() == {}


def test_rapl_reader_reads_domain_microjoules(tmp_path):
    base = _make_rapl(tmp_path, energy_uj=5_000_000)
    r = RaplSysfsReader(base=base)
    assert r.available() is True
    readings = r.read_domains_uj()
    assert readings["intel-rapl:0"] == 5_000_000


def test_rapl_delta_joules_simple(tmp_path):
    start = {"intel-rapl:0": 1_000_000}
    end = {"intel-rapl:0": 4_000_000}
    max_range = {"intel-rapl:0": 262143328850}
    # 3_000_000 uJ == 3.0 J
    assert rapl_delta_joules(start, end, max_range) == 3.0


def test_rapl_delta_joules_handles_counter_wraparound(tmp_path):
    # end < start means the monotonic counter wrapped past max_energy_range_uj
    start = {"d": 262143328850 - 1_000_000}
    end = {"d": 2_000_000}
    max_range = {"d": 262143328850}
    # true delta = (max - start) + end = 1_000_000 + 2_000_000 = 3_000_000 uJ
    assert rapl_delta_joules(start, end, max_range) == 3.0


def test_probe_counters_reports_rapl_when_present(tmp_path):
    base = _make_rapl(tmp_path, energy_uj=1)
    counters = probe_counters(rapl_base=base, allow_nvml=False)
    assert CounterName.RAPL_PKG in counters


def test_probe_counters_empty_on_bare_host(tmp_path):
    counters = probe_counters(rapl_base=tmp_path / "none", allow_nvml=False)
    assert counters == []


def test_rapl_max_range_uj_read(tmp_path):
    base = _make_rapl(tmp_path, energy_uj=1, max_range_uj=262143328850)
    r = RaplSysfsReader(base=base)
    assert r.max_range_uj()["intel-rapl:0"] == 262143328850


def test_probe_counters_reports_dram_domain(tmp_path):
    base = tmp_path / "powercap"
    dom = base / "intel-rapl:0:0"
    dom.mkdir(parents=True)
    (dom / "name").write_text("dram\n")
    (dom / "energy_uj").write_text("123\n")
    (dom / "max_energy_range_uj").write_text("262143328850\n")
    counters = probe_counters(rapl_base=base, allow_nvml=False)
    assert CounterName.RAPL_PKG in counters
    assert CounterName.RAPL_DRAM in counters


def test_rapl_reader_skips_unreadable_domain(tmp_path):
    base = tmp_path / "powercap"
    dom = base / "intel-rapl:0"
    dom.mkdir(parents=True)
    # energy_uj exists but holds garbage → unreadable, must not raise
    (dom / "energy_uj").write_text("not-a-number\n")
    r = RaplSysfsReader(base=base)
    assert r.available() is False
    assert r.read_domains_uj() == {}
