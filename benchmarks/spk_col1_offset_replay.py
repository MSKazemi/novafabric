"""SPK-COL-1 — offset-replay rebuild spike (ADR-0020 gate, gap-002).

Proves the durable-buffer property the collector design depends on: a
downstream store can be **fully rebuilt by replaying the buffer from offset
0** after deletion, with per-``run_id`` partition ordering preserved, and the
buffer survives a broker restart (RF1 single-replica regime) without loss.

Buffer: NATS JetStream (file storage), subjects ``spk.col1.<run_id>`` —
run_id-keyed partitioning per SI-1. Downstream store: a local object-store
stand-in (one append-file per run_id + sha256 digest), which is the honest
minimal materialization — the property under test is the BUFFER's
replayability, not any specific store's ingestion logic.

Acceptance (spike record):
  1. rebuilt store byte-equal on per-capsule digests;
  2. partition order per run_id preserved (event seq strictly increasing);
  3. RF1 survives a broker restart + replay (no data loss).

Usage (on n1, NATS at localhost:4222, prod docker profile)::

    .venv/bin/python benchmarks/spk_col1_offset_replay.py \
        --runs 50 --events-per-run 200 [--restart-cmd "docker restart novafabric-nats"]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import nats
from nats.js.api import ConsumerConfig, DeliverPolicy, StorageType, StreamConfig

STREAM = "SPK_COL1"
SUBJECT_PREFIX = "spk.col1"


def _event(run_id: str, seq: int) -> bytes:
    return json.dumps(
        {
            "schema": "envelope-v1-spike",
            "run_id": run_id,
            "seq": seq,
            "ts": f"2026-06-12T00:00:{seq % 60:02d}Z",
            "payload": hashlib.sha256(f"{run_id}:{seq}".encode()).hexdigest(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _materialize_digests(store_dir: Path) -> dict[str, str]:
    return {
        f.stem: hashlib.sha256(f.read_bytes()).hexdigest()
        for f in sorted(store_dir.glob("*.jsonl"))
    }


async def _drain_to_store(js, store_dir: Path, expected: int) -> tuple[int, bool]:
    """Replay the stream from offset 0 into *store_dir* (ephemeral pull consumer).

    Returns (events_replayed, per-run order preserved).
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    seen_seq: dict[str, int] = {}
    ordered = True
    count = 0
    sub = await js.pull_subscribe(
        f"{SUBJECT_PREFIX}.>",
        stream=STREAM,
        config=ConsumerConfig(deliver_policy=DeliverPolicy.ALL),
    )
    files: dict[str, object] = {}
    try:
        while count < expected:
            batch = await sub.fetch(min(500, expected - count), timeout=10)
            if not batch:
                break
            for msg in batch:
                record = json.loads(msg.data)
                run_id = record["run_id"]
                if record["seq"] <= seen_seq.get(run_id, -1):
                    ordered = False
                seen_seq[run_id] = record["seq"]
                fh = files.get(run_id)
                if fh is None:
                    fh = open(store_dir / f"{run_id}.jsonl", "ab")
                    files[run_id] = fh
                fh.write(msg.data + b"\n")  # type: ignore[union-attr]
                count += 1
                await msg.ack()
    finally:
        for fh in files.values():
            fh.close()  # type: ignore[union-attr]
        await sub.unsubscribe()
    return count, ordered


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nats-url", default="nats://127.0.0.1:4222")
    parser.add_argument("--runs", type=int, default=50)
    parser.add_argument("--events-per-run", type=int, default=200)
    parser.add_argument(
        "--restart-cmd",
        default=None,
        help='broker restart command for the RF1 leg, e.g. "docker restart novafabric-nats"',
    )
    args = parser.parse_args()
    total = args.runs * args.events_per_run

    nc = await nats.connect(args.nats_url)
    js = nc.jetstream()
    try:
        await js.delete_stream(STREAM)
    except Exception:
        pass
    await js.add_stream(
        StreamConfig(
            name=STREAM,
            subjects=[f"{SUBJECT_PREFIX}.>"],
            storage=StorageType.FILE,
            num_replicas=1,  # RF1 — the regime under test
        )
    )

    # 1) Ingest, run_id-keyed
    t0 = time.perf_counter()
    for r in range(args.runs):
        run_id = f"run{r:04d}"
        for s in range(args.events_per_run):
            await js.publish(f"{SUBJECT_PREFIX}.{run_id}", _event(run_id, s))
    ingest_s = time.perf_counter() - t0
    print(f"ingested {total} events ({args.runs} runs) in {ingest_s:.1f}s "
          f"({total/ingest_s:,.0f} ev/s)")

    work = Path(tempfile.mkdtemp(prefix="spk_col1_"))

    # 2) First materialization + digest snapshot
    n1_, ord1 = await _drain_to_store(js, work / "store1", total)
    digests1 = _materialize_digests(work / "store1")
    print(f"materialization #1: {n1_} events, order_preserved={ord1}, "
          f"{len(digests1)} run digests")

    # 3) Delete downstream store; replay from offset 0
    shutil.rmtree(work / "store1")
    n2_, ord2 = await _drain_to_store(js, work / "store2", total)
    digests2 = _materialize_digests(work / "store2")
    rebuilt_equal = digests1 == digests2
    print(f"rebuild after delete: {n2_} events, order_preserved={ord2}, "
          f"digests byte-equal={rebuilt_equal}")

    # 4) RF1 broker-restart leg
    restart_equal = None
    if args.restart_cmd:
        await nc.close()
        print(f"restarting broker: {args.restart_cmd}")
        subprocess.run(args.restart_cmd, shell=True, check=True, timeout=120)
        time.sleep(5)
        for attempt in range(12):
            try:
                nc = await nats.connect(args.nats_url)
                break
            except Exception:
                time.sleep(5)
        js = nc.jetstream()
        # Losing the stream outright is the *loudest* durability failure there
        # is, and it used to escape as an unhandled NotFoundError -- a crash
        # that a caller reading the verdict line never sees. A non-durable
        # broker must report FAIL here, not a traceback.
        try:
            n3_, ord3 = await _drain_to_store(js, work / "store3", total)
        except Exception as exc:  # noqa: BLE001 - any drain failure is a FAIL
            restart_equal = False
            print(f"rebuild after broker restart: FAILED to drain -- {exc!r}")
            print("  the stream did not survive the restart: this is the "
                  "durability property under test, reported as FAIL")
        else:
            digests3 = _materialize_digests(work / "store3")
            restart_equal = digests1 == digests3
            print(f"rebuild after broker restart: {n3_} events, "
                  f"order_preserved={ord3}, digests byte-equal={restart_equal}")

    # Teardown must not be able to turn a reported result into a traceback. A
    # stream that is already gone is exactly the state the durability failure
    # leaves behind, so deleting it 404s -- and that 404 was escaping *after*
    # the FAIL had been determined but *before* it could be printed.
    try:
        await js.delete_stream(STREAM)
    except Exception as exc:  # noqa: BLE001 - cleanup must never mask the verdict
        print(f"cleanup: delete_stream skipped ({exc!r})")
    await nc.close()
    shutil.rmtree(work, ignore_errors=True)

    # A skipped check is not a passed check. `restart_equal is None` (no
    # --restart-cmd given) used to satisfy this conjunction, so the harness
    # printed "verdict: PASS" for a run in which the durability arm never
    # executed -- and a paper drafted from that log claimed crash durability
    # nothing had measured. The overall verdict is now PASS only when every arm
    # ran and passed; a skipped arm downgrades it to INCOMPLETE, which is
    # neither a pass nor a failure and cannot be mistaken for either.
    ran_all = restart_equal is not None
    passed = rebuilt_equal and ord1 and ord2 and (restart_equal is not False)
    checks = [
        f"byte-equal rebuild: {'PASS' if rebuilt_equal else 'FAIL'}",
        f"per-run order: {'PASS' if (ord1 and ord2) else 'FAIL'}",
        "RF1 restart: "
        + ("SKIPPED" if restart_equal is None else "PASS" if restart_equal else "FAIL"),
    ]
    if not passed:
        verdict = "FAIL"
    elif not ran_all:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"
    print(f"SPK-COL-1 verdict: {verdict} — " + "; ".join(checks))
    if verdict == "INCOMPLETE":
        print("  pass --restart-cmd to run the RF1 broker-restart arm; without it "
              "no crash-durability claim is supported by this run")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
