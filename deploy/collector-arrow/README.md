# OTel-Arrow wire profile (experimental, ADR-0020 / SI-3)

Config-driven Arrow transport for the spool→central hop, using the official
`otelcol-contrib` distribution (v0.154.0+, bundles `otelarrow`
exporter/receiver). No NovaFabric code changes — drop these configs into an
existing collector deployment.

**Measured (SPK-COL-3, n1, 2026-06-12):** 31.5 % egress reduction vs
OTLP+zstd on the identical corpus and batch settings (`benchmarks/spk_col3/`
holds the reproducible A/B harness); sender memory bounded under a 10× burst
(max RSS 237 MiB). Synthetic-corpus caveat: repetitive spans favor row-wise
zstd, so real workloads typically see more than 31.5 % (upstream reports
30–70 %).

## Run

```bash
# central side
otelcol-contrib --config receiver.yaml
# each node
otelcol-contrib --config sender.yaml   # set exporters.otelarrow.endpoint first
```

`disable_downgrade: true` makes a misconfigured pair fail loudly instead of
silently falling back to plain OTLP — keep it on so you know which wire
you're actually paying for.

Status: **experimental** — deployment profile only; the resident-emitter
integration into the NovaFabric collector is the Phase-2 build (tracked in
BUILD_QUEUE).
