"""SPK-COL-2 — per-event hot-path overhead spike (ADR-0020 gate, gap-002).

Measures the marginal cost of the capture plane per intercepted call in a
*warm* process — exactly what the SPK-COL-2 spike record asks for after the
cold-start finding (the end-to-end harness conflates ~3 s CLI startup with
the per-event hot path).

Method
------
1. Start a local mock-LLM HTTP server whose handler sleeps ``--latency-ms``
   (default 100 ms — the "LLM calls >= 100 ms each" regime where the <= 5 %
   per-call budget applies).
2. Baseline: N ``requests.post`` calls against it, no hooks.
3. Instrumented: install the wire-level capture hooks
   (``capture.hooks.install_all`` with a real ``CapsuleWriter``) in the same
   warm process and run N identical calls.
4. Report per-call medians and the marginal overhead, absolute and as a
   percentage of the call latency.

Acceptance threshold (ADR-0020 / SPK-COL-2): hot-path overhead <= 4 %
(target <= 2 %) of wall-clock on the representative workload.

Usage::

    uv run python benchmarks/spk_col2_hotpath.py --n 200 --latency-ms 100
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


class _MockLLMHandler(BaseHTTPRequestHandler):
    latency_s = 0.1

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        time.sleep(self.latency_s)
        body = json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:  # silence
        pass


def _run_calls(url: str, n: int, session: requests.Session) -> list[float]:
    samples: list[float] = []
    payload = {"model": "mock", "messages": [{"role": "user", "content": "hi"}]}
    for _ in range(n):
        t0 = time.perf_counter()
        session.post(url, json=payload, timeout=30)
        samples.append(time.perf_counter() - t0)
    return samples


def _stats(label: str, samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    result = {
        "median_ms": statistics.median(ordered) * 1000,
        "p95_ms": ordered[int(len(ordered) * 0.95) - 1] * 1000,
        "mean_ms": statistics.fmean(ordered) * 1000,
    }
    print(
        f"{label:>14}: median {result['median_ms']:8.2f} ms   "
        f"p95 {result['p95_ms']:8.2f} ms   mean {result['mean_ms']:8.2f} ms"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=200, help="calls per arm")
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--latency-ms", type=float, default=100.0)
    args = parser.parse_args()

    _MockLLMHandler.latency_s = args.latency_ms / 1000.0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockLLMHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"

    session = requests.Session()
    _run_calls(url, args.warmup, session)  # connection + interpreter warmup

    baseline = _run_calls(url, args.n, session)
    base = _stats("baseline", baseline)

    # Instrument the SAME warm process: real CapsuleWriter + wire hooks.
    # Register the mock endpoint via the env-var path the URL registry
    # honours (OLLAMA_BASE_URL) so the wire hook actually records.
    import os

    os.environ["OLLAMA_BASE_URL"] = f"http://127.0.0.1:{server.server_port}"

    from novafabric.capture.capsule import CapsuleWriter
    from novafabric.capture.hooks import install_all

    tmp = tempfile.mkdtemp(prefix="spk_col2_")
    writer = CapsuleWriter(run_id="spk-col2", base_dir=Path(tmp))
    writer.open()
    capsule_dir = Path(tmp) / "spk-col2"
    install_all(writer, parent_span_id="spk-col2")

    _run_calls(url, args.warmup, session)  # hook warmup
    instrumented = _run_calls(url, args.n, session)
    inst = _stats("instrumented", instrumented)

    events = 0
    for name in ("network_events.jsonl", "model-calls.jsonl", "tool-calls.jsonl"):
        path = capsule_dir / name
        if path.exists():
            events += sum(1 for line in path.read_text().splitlines() if line.strip())

    overhead_ms = inst["median_ms"] - base["median_ms"]
    overhead_pct = 100.0 * overhead_ms / base["median_ms"]
    print(
        f"\nper-call marginal overhead (median): {overhead_ms:+.3f} ms "
        f"({overhead_pct:+.2f} % of a {base['median_ms']:.1f} ms call)"
    )
    print(f"captured events written: {events} (capsule: {capsule_dir})")
    verdict = (
        "PASS (<= 2 % target)"
        if overhead_pct <= 2.0
        else "PASS (<= 4 % budget)"
        if overhead_pct <= 4.0
        else "FAIL (> 4 % budget)"
    )
    print(f"SPK-COL-2 hot-path verdict: {verdict}")
    server.shutdown()
    return 0 if overhead_pct <= 4.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
