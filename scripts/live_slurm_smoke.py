"""Live SlurmRunner smoke test — minimal, no pytest dependency.

Run from a SLURM submit node where:
  - sbatch + sacct are on PATH
  - novafabric is importable (this script's interpreter)
  - the chosen --output-dir is on a filesystem shared with the
    compute nodes that SLURM may allocate

Usage::

    /path/to/venv/bin/python live_slurm_smoke.py [partition] [output_dir]

Defaults: partition=debug, output_dir=/home/$USER/nova-slurm-smoke
"""
from __future__ import annotations

import sys
from pathlib import Path

from novafabric.runners import RunnerJobSpec, SlurmRunner


def main() -> int:
    partition = sys.argv[1] if len(sys.argv) > 1 else "debug"
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else (
        Path.home() / "nova-slurm-smoke"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    print("== Live SlurmRunner smoke ==")
    print(f"  partition  : {partition}")
    print(f"  output_dir : {out_dir}")
    print(f"  python     : {sys.executable}")
    print()

    runner = SlurmRunner()
    ok, why = runner.supports()
    print(f"runner.supports() -> ({ok}, {why!r})")
    if not ok:
        print(f"FAIL: SLURM tooling not usable: {why}", file=sys.stderr)
        return 1

    spec = RunnerJobSpec(
        run_id="01LIVESLURMSMOKE000000000000",
        command=["echo", "hello-from-slurm"],
        capsule_dir=out_dir,
        env={
            "NOVAFABRIC_CAPSULE_DIR": str(out_dir),
            "NOVAFABRIC_SPAN_ID": "0" * 16,
        },
        runner_options={
            "partition": partition,
            "time": "00:01:00",
        },
        timeout_s=120.0,
    )

    print("Submitting job...")
    result = runner.run(spec)

    print()
    print("== Result ==")
    print(f"  exit_code      : {result.exit_code}")
    print(f"  runner_status  : {result.runner_status}")
    print(f"  runner_error   : {result.runner_error}")
    print(f"  job_id         : {result.runner_metadata.get('job_id')}")
    print(f"  final_state    : {result.runner_metadata.get('final_state')}")
    print(f"  sacct_exit     : {result.runner_metadata.get('sacct_exit_code')}")
    print()
    print(f"  stdout ({len(result.stdout)} bytes):")
    print(result.stdout.decode(errors="replace") or "  (empty)")
    print()
    if result.stderr:
        print(f"  stderr ({len(result.stderr)} bytes):")
        print(result.stderr.decode(errors="replace"))
        print()

    if result.exit_code == 0 and b"hello-from-slurm" in result.stdout:
        print("PASS: live SlurmRunner smoke succeeded")
        return 0
    print("FAIL: live SlurmRunner smoke did not produce expected output", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
