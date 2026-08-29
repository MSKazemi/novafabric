"""A deliberately boring workload, so the example is about the capsule.

Standard library only: no GPU, no API key, no network, no private registry.
It prints a deterministic line and exits 0, which is everything the example
needs from it. Making this realistic would make the example worse — a reader
should be able to run the whole thing in a few seconds and spend their
attention on what ends up in the capsule.
"""

from __future__ import annotations

import os
import platform
import sys


def main() -> int:
    print("payload: hello from the container")
    # These three are the interesting ones: they are what lets a reader see,
    # from inside the capsule, that the workload really ran in the container
    # and not on the host.
    print(f"payload: python   = {sys.version.split()[0]}")
    print(f"payload: platform = {platform.platform()}")
    print(f"payload: hostname = {platform.node()}")
    # Set by the orchestrator for every runner. Present inside the container
    # because the docker runner passes the prepared env through.
    print(f"payload: capsule  = {os.environ.get('NOVAFABRIC_CAPSULE_DIR', '<unset>')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
