"""Byte-counting TCP proxy for the SPK-COL-3 wire A/B (ADR-0020 gate).

Forwards 127.0.0.1:<listen> -> 127.0.0.1:<target> and counts bytes in both
directions. The client->server direction is the spool->central egress under
test. Counts are written to --out as JSON every second and on shutdown.

Both arms (OTLP+zstd and OTel-Arrow) traverse the same proxy, so HTTP/2
framing and gRPC keepalive overhead cancel out in the comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
from pathlib import Path

totals = {"client_to_server": 0, "server_to_client": 0, "connections": 0}


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, key: str) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            totals[key] += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle(client_r, client_w, target_port: int) -> None:
    totals["connections"] += 1
    try:
        server_r, server_w = await asyncio.open_connection("127.0.0.1", target_port)
    except OSError:
        client_w.close()
        return
    await asyncio.gather(
        _pump(client_r, server_w, "client_to_server"),
        _pump(server_r, client_w, "server_to_client"),
    )


async def _flush_loop(out: Path) -> None:
    while True:
        out.write_text(json.dumps(totals) + "\n")
        await asyncio.sleep(1)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", type=int, required=True)
    parser.add_argument("--target", type=int, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, args.target), "127.0.0.1", args.listen
    )
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    flusher = asyncio.create_task(_flush_loop(args.out))
    async with server:
        await stop.wait()
    flusher.cancel()
    args.out.write_text(json.dumps(totals) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
