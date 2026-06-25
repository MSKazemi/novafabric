import subprocess
import sys


def test_importing_novafabric_starts_no_background_threads():
    """Prefork is only safe if importing ``novafabric`` (and the orchestrator
    module) spawns no import-time threads. Run in a clean subprocess and assert
    only MainThread is alive after import. This is a hard gate on the warm
    capture daemon's prefork model (ADR-0092)."""
    code = (
        "import threading, novafabric, novafabric.capture.orchestrator;"
        "print(','.join(t.name for t in threading.enumerate()))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    names = out.stdout.strip().split(",")
    assert names == ["MainThread"], f"import-time threads present: {names}"
