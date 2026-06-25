"""Energy counter readers (ADR-0093 §A1).

RAPL via ``/sys/class/powercap`` is stdlib-only (no dependency, Tier-A per
ADR-0024). NVML is optional and lazy-imported behind the ``[energy-gpu]`` extra.
Every reader degrades safely: an unreadable counter yields an empty reading and
**never raises** into the workload (the "capture never blocks" invariant).

n1 is CPU-only, so the RAPL path is the well-tested default; NVML is validated
out-of-band on a GPU box.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from novafabric.energy._receipt import CounterName

_RAPL_BASE = Path("/sys/class/powercap")


@runtime_checkable
class EnergyReader(Protocol):
    """A source of cumulative energy counters, in microjoules per domain."""

    name: str

    def available(self) -> bool: ...

    def read_domains_uj(self) -> dict[str, int]: ...


class NullReader:
    """Always-unavailable reader — the honest no-counter default."""

    name = "null"

    def available(self) -> bool:
        return False

    def read_domains_uj(self) -> dict[str, int]:
        return {}


class RaplSysfsReader:
    """Reads Intel RAPL energy counters from ``/sys/class/powercap``.

    Each ``intel-rapl:*`` domain exposes a monotonic ``energy_uj`` counter that
    wraps at ``max_energy_range_uj``; :func:`rapl_delta_joules` handles the wrap.
    """

    name = "rapl"

    def __init__(self, base: Path = _RAPL_BASE) -> None:
        self.base = Path(base)

    def _domains(self) -> list[Path]:
        if not self.base.is_dir():
            return []
        return sorted(
            d
            for d in self.base.glob("intel-rapl:*")
            if (d / "energy_uj").exists()
        )

    def available(self) -> bool:
        for dom in self._domains():
            try:
                int((dom / "energy_uj").read_text().strip())
                return True
            except (OSError, ValueError):
                continue
        return False

    def read_domains_uj(self) -> dict[str, int]:
        readings: dict[str, int] = {}
        for dom in self._domains():
            try:
                readings[dom.name] = int((dom / "energy_uj").read_text().strip())
            except (OSError, ValueError):
                continue
        return readings

    def max_range_uj(self) -> dict[str, int]:
        ranges: dict[str, int] = {}
        for dom in self._domains():
            path = dom / "max_energy_range_uj"
            try:
                ranges[dom.name] = int(path.read_text().strip())
            except (OSError, ValueError):
                continue
        return ranges


def rapl_delta_joules(
    start_uj: dict[str, int],
    end_uj: dict[str, int],
    max_range_uj: dict[str, int],
) -> float:
    """Energy consumed between two RAPL readings, in joules, wraparound-safe.

    For each domain present in both readings, the delta is ``end - start``; if
    the counter wrapped (``end < start``) the domain's ``max_energy_range_uj`` is
    added once. Domains are summed.
    """
    total_uj = 0
    for domain, start in start_uj.items():
        if domain not in end_uj:
            continue
        end = end_uj[domain]
        delta = end - start
        if delta < 0:
            delta += max_range_uj.get(domain, 0)
        if delta >= 0:
            total_uj += delta
    return total_uj / 1_000_000


def _nvml_available() -> bool:
    try:
        import pynvml  # noqa: F401
    except Exception:  # pragma: no cover - depends on optional extra
        return False
    try:  # pragma: no cover - requires a GPU
        pynvml.nvmlInit()
        ok = pynvml.nvmlDeviceGetCount() > 0
        pynvml.nvmlShutdown()
        return bool(ok)
    except Exception:  # pragma: no cover
        return False


def probe_counters(
    *,
    rapl_base: Path = _RAPL_BASE,
    allow_nvml: bool = True,
) -> list[CounterName]:
    """Report which energy counters are readable on this host.

    This is the ground truth the forgery guard checks against: a ``measured``
    receipt must name a counter that ``probe_counters`` actually found.
    """
    found: list[CounterName] = []
    rapl = RaplSysfsReader(base=rapl_base)
    if rapl.available():
        found.append(CounterName.RAPL_PKG)
        # DRAM domains, when the platform exposes them
        for dom in rapl._domains():
            try:
                label = (dom / "name").read_text().strip().lower()
            except OSError:
                continue
            if "dram" in label:
                found.append(CounterName.RAPL_DRAM)
                break
    if allow_nvml and _nvml_available():  # pragma: no cover - requires a GPU
        found.append(CounterName.NVML)
    return found


__all__ = [
    "EnergyReader",
    "NullReader",
    "RaplSysfsReader",
    "probe_counters",
    "rapl_delta_joules",
]
