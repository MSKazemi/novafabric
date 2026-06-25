#!/usr/bin/env bash
# SPK-COL-3 — OTel-Arrow vs OTLP+zstd wire A/B (ADR-0020 gate, gap-002).
#
# Runs the same telemetrygen trace corpus through two sender->receiver
# collector pipelines that differ ONLY in wire protocol, with a
# byte-counting TCP proxy on the egress hop. Acceptance: OTel-Arrow egress
# >= 30 % smaller at equal fidelity; sender memory bounded under a 10x burst.
#
# Prereqs (n1): ./otelcol-contrib (v0.154.0) in $WORK, docker (telemetrygen
# image pulled), python3.
set -euo pipefail

WORK="${WORK:-$HOME/spk-col3}"
KIT="$(cd "$(dirname "$0")" && pwd)"
TRACES="${TRACES:-20000}"
BURST_TRACES=$((TRACES * 10))
CHILD_SPANS="${CHILD_SPANS:-4}"
cd "$WORK"

write_configs() {
cat > sender_otlp.yaml <<'EOF'
receivers:
  otlp: {protocols: {grpc: {endpoint: 127.0.0.1:15317}}}
processors:
  batch: {send_batch_size: 8192, timeout: 500ms}
exporters:
  otlp:
    endpoint: 127.0.0.1:15400
    tls: {insecure: true}
    compression: zstd
service:
  telemetry: {metrics: {level: none}, logs: {level: error}}
  pipelines:
    traces: {receivers: [otlp], processors: [batch], exporters: [otlp]}
EOF
cat > sender_arrow.yaml <<'EOF'
receivers:
  otlp: {protocols: {grpc: {endpoint: 127.0.0.1:15317}}}
processors:
  batch: {send_batch_size: 8192, timeout: 500ms}
exporters:
  otelarrow:
    endpoint: 127.0.0.1:15400
    tls: {insecure: true}
    compression: zstd
    arrow:
      num_streams: 1
      disable_downgrade: true
service:
  telemetry: {metrics: {level: none}, logs: {level: error}}
  pipelines:
    traces: {receivers: [otlp], processors: [batch], exporters: [otelarrow]}
EOF
cat > receiver_otlp.yaml <<'EOF'
receivers:
  otlp: {protocols: {grpc: {endpoint: 127.0.0.1:15500}}}
exporters:
  nop: {}
service:
  telemetry: {metrics: {level: none}, logs: {level: error}}
  pipelines:
    traces: {receivers: [otlp], exporters: [nop]}
EOF
cat > receiver_arrow.yaml <<'EOF'
receivers:
  otelarrow: {protocols: {grpc: {endpoint: 127.0.0.1:15500}}}
exporters:
  nop: {}
service:
  telemetry: {metrics: {level: none}, logs: {level: error}}
  pipelines:
    traces: {receivers: [otelarrow], exporters: [nop]}
EOF
}

PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; PIDS=(); sleep 1; }
trap cleanup EXIT

gen() { # $1 = n traces
  docker run --rm --network host \
    ghcr.io/open-telemetry/opentelemetry-collector-contrib/telemetrygen:latest \
    traces --otlp-insecure --otlp-endpoint 127.0.0.1:15317 \
    --traces "$1" --child-spans "$CHILD_SPANS" --workers 4 --rate 0 \
    --otlp-attributes='run_id="spk-col3"' >/dev/null 2>&1
}

run_arm() { # $1 = otlp|arrow, $2 = traces, $3 = rss-sample(bool)
  local arm="$1" n="$2" rss="$3"
  rm -f "bytes_$arm.json"
  ./otelcol-contrib --config "receiver_$arm.yaml" >"recv_$arm.log" 2>&1 & PIDS+=($!)
  python3 "$KIT/byte_proxy.py" --listen 15400 --target 15500 --out "bytes_$arm.json" & PIDS+=($!)
  ./otelcol-contrib --config "sender_$arm.yaml" >"send_$arm.log" 2>&1 & local SENDER=$!; PIDS+=($SENDER)
  sleep 4
  local rss_max=0
  if [ "$rss" = "yes" ]; then
    ( while kill -0 $SENDER 2>/dev/null; do
        r=$(awk '/VmRSS/{print $2}' /proc/$SENDER/status 2>/dev/null || echo 0)
        [ "${r:-0}" -gt "$rss_max" ] 2>/dev/null && rss_max=$r
        echo "$rss_max" > "rss_$arm.max"; sleep 0.5
      done ) & PIDS+=($!)
  fi
  gen "$n"
  sleep 8   # let the batch queue flush through the proxy
  cleanup
  trap cleanup EXIT
}

write_configs
echo "== arm A: OTLP + zstd ($TRACES traces x $((CHILD_SPANS+1)) spans)"
run_arm otlp "$TRACES" no
A=$(python3 -c "import json;print(json.load(open('bytes_otlp.json'))['client_to_server'])")
echo "   egress bytes: $A"

echo "== arm B: OTel-Arrow ($TRACES traces x $((CHILD_SPANS+1)) spans)"
run_arm arrow "$TRACES" no
B=$(python3 -c "import json;print(json.load(open('bytes_arrow.json'))['client_to_server'])")
echo "   egress bytes: $B"

echo "== burst leg: OTel-Arrow, 10x corpus ($BURST_TRACES traces), sender RSS sampled"
run_arm arrow "$BURST_TRACES" yes
BURST=$(python3 -c "import json;print(json.load(open('bytes_arrow.json'))['client_to_server'])")
RSS_KB=$(cat rss_arrow.max 2>/dev/null || echo 0)

python3 - "$A" "$B" "$BURST" "$RSS_KB" <<'EOF'
import sys
a, b, burst, rss_kb = map(int, sys.argv[1:5])
red = 100.0 * (a - b) / a if a else 0.0
print(f"\nSPK-COL-3 results")
print(f"  OTLP+zstd egress : {a:>12,} bytes")
print(f"  OTel-Arrow egress: {b:>12,} bytes")
print(f"  reduction        : {red:.1f} %  (acceptance: >= 30 %)")
print(f"  burst egress 10x : {burst:,} bytes; sender max RSS {rss_kb/1024:.0f} MiB")
ok = red >= 30.0 and rss_kb < 2_000_000  # < ~2 GiB = bounded
print("SPK-COL-3 verdict: " + ("PASS" if ok else "FAIL"))
sys.exit(0 if ok else 1)
EOF
