# Collector reference: the Go binaries

This page covers the Go side of the collector tier in `collector/`: the
`novafabric-spool-forwarder`, `novafabric-collector` and `novafabric-verifier` binaries,
the `libnovaspool` shared library, and the OpenTelemetry Collector Builder (OCB)
configuration. For each one it gives the usage, every flag with its default, the
environment variables, exit codes, the files it reads or writes, and a maturity label.
Everything here was checked against the source in `collector/cmd/*/main.go`,
`collector/internal/` and `collector/pkg/`.

The Python side of the same tier (`nova capture --emit-spool`, `nova collector rebuild`)
is in the [CLI reference](cli-reference.md). `novafabric-hpc-hub` is documented there
too, in [its own section](cli-reference.md#novafabric-hpc-hub).

## Maturity at a glance

| Component | Maturity | What works today |
|---|---|---|
| `novafabric-spool-forwarder` | **experimental** | Drains a local spool into a NATS JetStream stream. It publishes unsigned envelopes |
| `novafabric-verifier` | **experimental** | Checks the NovaSeal Ed25519 signature on one OTLP `ResourceLogs` batch offline |
| `novafabric-collector` | **experimental**, cannot run a pipeline | Loads and checks a config (`file:`, `env:`, `yaml:`); `--help`, `--version`, `components` and `print-config` work. With no receiver or exporter built in, a pipeline cannot start, see [below](#novafabric-collector) |
| `novaseal_batch_signer` processor (inside the collector) | **experimental** (OTel stability `Alpha`) | Signs batches with the local dev key; unit-tested, but no shipped binary can run it yet |
| `libnovaspool.so` (C ABI, `collector/pkg/cffi`) | **experimental** | Writes spool segments from C or Python; built by hand, not shipped |
| OCB build (`collector/ocb/builder-config.yaml`) | **future design** | A builder config file only. CI does not build it, and as written it does not compile ([details](#ocb-build-config)) |
| Hub signing of forwarded events | **future design** | Not implemented: nothing signs what the forwarder publishes |

None of these binaries ships in a release. There is no published image, package or
release tag for the `collector/` module yet; you build them from a checkout.

## How it fits together

```text
 compute node                                      cluster / service side
 ------------                                      ----------------------
 nova capture --emit-spool ─┐
 NovaPySpool (Python)       ├─► spool dir ─► novafabric-spool-forwarder ─► NATS JetStream
 libnovaspool.so (C ABI)   ─┘   <seq>.jsonl      (drain, publish, ack)       stream NOVA_EVIDENCE
                                checkpoint.json                              subjects nova.evidence.<run_id>
                                                                                    │
                                    consumers (experimental) ◄──────────────────────┘
                                    · nova collector rebuild --stream NOVA_EVIDENCE
                                    · novafabric.evidence_fabric.NATSJetStreamConsumer

 OTLP logs ─► novafabric-collector [novaseal_batch_signer] ─► exporter      (cannot run today)
 signed ResourceLogs batch file ─► novafabric-verifier ─► OK / INVALID       (offline)
```

1. **Spool (works today, experimental).** `nova capture --emit-spool` writes run-boundary
   EventEnvelope v1 records into the spool directory (`$NOVAFABRIC_SPOOL_DIR`, default
   `$NOVAFABRIC_HOME/spool`) through `NovaPySpool`. `NovaPySpool` uses `libnovaspool.so`
   when it can load it and a pure-Python writer with the same on-disk format when it
   cannot.
2. **Forwarder (experimental).** `novafabric-spool-forwarder` reads committed segments,
   publishes each event to `<subject-prefix>.<run_id>` on a JetStream stream, waits for
   the server's ack, then advances `checkpoint.json` and deletes the segment files it has
   fully forwarded. The envelopes are **unsigned**: compute nodes hold no signing key
   (the hub-sign design), and the hub that would sign them is not implemented.
3. **Consumers (experimental).** `nova collector rebuild` replays the stream from offset
   0 into per-run JSONL files. The `NATSJetStreamConsumer` class in
   `novafabric.evidence_fabric` pulls from the stream into an accumulator. No `nova`
   command starts that consumer for you.
4. **Signing and verification.** The `novaseal_batch_signer` processor signs OTLP batches,
   but the only binary that registers it (`novafabric-collector`) has no receiver or
   exporter to put it in a pipeline. `novafabric-verifier` checks a signed batch offline.

**Stream name.** The evidence stream is `NOVA_EVIDENCE` on both sides: the forwarder
creates it, and `nova collector rebuild` and `NATSJetStreamConsumer` read it by default.
The subject filter is `nova.evidence.>`. Change the forwarder's name with `--stream` or
`NOVAFABRIC_SPOOL_STREAM`, and the consumers' with `--stream` or `NOVA_NATS_STREAM`; set
both to the same value. Releases before this change defaulted the Python consumers to
`nova-evidence`; if you created a stream by that name yourself and relied on the old
default, set `NOVA_NATS_STREAM=nova-evidence`.

## Building

You need Go 1.26.5 or newer (see `collector/go.mod`).

```bash
cd collector
go build -o bin/novafabric-spool-forwarder ./cmd/novafabric-spool-forwarder
go build -o bin/novafabric-collector       ./cmd/novafabric-collector
go build -o bin/novafabric-verifier        ./cmd/novafabric-verifier

# libnovaspool needs cgo and a C toolchain
go build -buildmode=c-shared -o ../lib/libnovaspool.so ./pkg/cffi/
```

`make build` in `collector/` builds the collector, the verifier and the hub stub, but not
the spool forwarder or `libnovaspool`. The `collector-ci` workflow builds the same three.

---

## novafabric-spool-forwarder

**experimental.** Drains the local event spool and publishes each EventEnvelope v1
record to a NATS JetStream stream. Source: `collector/cmd/novafabric-spool-forwarder`.

```bash
novafabric-spool-forwarder --spool /var/spool/novafabric --nats nats://127.0.0.1:4222
NOVAFABRIC_SPOOL_DIR=$NOVAFABRIC_HOME/spool novafabric-spool-forwarder
```

Flags use Go's `flag` package, so `-spool` and `--spool` are the same flag.

| Flag | Default | Env var | Description |
|---|---|---|---|
| `--spool DIR` | — (required) | `NOVAFABRIC_SPOOL_DIR` | Spool directory to drain. Created (mode 0750) if missing |
| `--nats URL` | `nats://127.0.0.1:4222` | `NOVA_NATS_URL` | NATS server URL |
| `--stream NAME` | `NOVA_EVIDENCE` | `NOVAFABRIC_SPOOL_STREAM` | JetStream stream. Created, or updated, at start to cover `<subject-prefix>.>` |
| `--subject-prefix P` | `nova.evidence` | `NOVAFABRIC_SPOOL_SUBJECT` | Each event goes to `<P>.<run_id>`; an event with no parseable `run_id` goes to `<P>.unknown` |
| `--interval D` | `500ms` | — | Pause between drain passes (Go duration: `500ms`, `2s`) |
| `--max-events N` | `1000` | — | Most events read and published in one pass |
| `--max-bytes N` | `1073741824` (1 GiB) | — | Spool size bound. **No effect in this process**: the bound is enforced when an event is written, and the forwarder only reads |

A flag given on the command line overrides its environment variable.

**Other environment.** `NOVA_DLQ_DIR` set to a path makes the spool open a dead-letter
directory there (created at start). The forwarder never writes to it, because only a
spool *write* routes an event to the dead-letter queue. `NOVAFABRIC_HPC_STORAGE` is read by
the NATS leaf-node code in the same Go package (`collector/internal/hpc`); the forwarder
never calls that code, so the variable has no effect on it.

**Behaviour.**

- Each pass reads up to `--max-events` events and publishes them one by one. Each publish
  waits up to 5 s for the JetStream ack, so a successful publish means the server stored
  the event.
- The checkpoint advances only after every event in the pass is published. If a publish
  fails, the process exits `1` without advancing it. After a restart the spool resumes
  from the first unacknowledged event, so delivery is at least once and consumers should
  deduplicate by `event_id`.
- Connecting to NATS and creating or updating the stream happen once at start (the
  stream step has a 10 s timeout). An unreachable server is an exit `1`, not a retry
  loop.
- SIGINT or SIGTERM stops it after the current pass, with exit `0`.
- Nothing in `deploy/` installs it as a service. Run it under a supervisor that restarts
  it on a non-zero exit (systemd `Restart=on-failure`, a Kubernetes container, and so on).

**Exit codes.**

| Code | Meaning |
|---|---|
| `0` | Stopped cleanly on SIGINT/SIGTERM; also `-h` / `--help` |
| `1` | The spool could not be opened, NATS or JetStream was unreachable, the stream could not be created, or a spool read or publish failed |
| `2` | Usage error: no spool directory (neither `--spool` nor `NOVAFABRIC_SPOOL_DIR`), or an unknown flag |

**Files** (in the spool directory):

| File | Written by | Meaning |
|---|---|---|
| `<seq>.jsonl` | the spool writers | One committed segment: a 16-byte header (writer PID, 64-bit sequence, complete flag, version 1, little-endian) followed by JSONL events. `<seq>` is 20 zero-padded digits |
| `<seq>[.<pid>].jsonl.tmp` | the spool writers | A segment being written. When the forwarder starts, a temp file older than 5 minutes is promoted if its header says the write completed, and deleted otherwise |
| `checkpoint.json` | the forwarder | The last acknowledged sequence and offset |

---

## novafabric-collector

**experimental, and it cannot run a pipeline today.** An OpenTelemetry Collector
distribution (`go.opentelemetry.io/collector/otelcol`) that registers the
`novaseal_batch_signer` processor. Source: `collector/cmd/novafabric-collector`.

What the binary registers:

- **Components:** the `novaseal_batch_signer` processor only. No receivers, exporters,
  connectors or extensions.
- **Config providers:** `file:`, `env:` and `yaml:`. A `--config` value without a scheme
  is a file path. `${NAME}` in a config expands from the environment. There is no
  `http:` or `https:` provider, so loading a config never touches the network.
- **Service telemetry:** the collector's own logs go to stderr as JSON. Internal metrics
  and traces are discarded: no metrics port is opened and nothing is sent anywhere.

So `--config` and `validate` read the file and check every section they know. Because a
pipeline needs a receiver and an exporter, a config that defines one is rejected with
`unknown type`, and a config without one fails with `no receiver configuration specified
in config`; both exit `1`. The reference config in `deploy/k8s/configmap-collector.yaml`
fails the same way (`otlp`, `batch` and `kafka` are not built in, and
`service::telemetry::metrics` is not accepted). For a smoke test of the config loader
and the service lifecycle, the upstream feature gate `service.AllowNoPipelines` lets it
start with no pipelines:

```bash
novafabric-collector --help
novafabric-collector --version        # novafabric-collector version 0.1.0
novafabric-collector components       # novaseal_batch_signer (logs: Alpha); providers env, file, yaml
novafabric-collector validate --config collector.yaml
novafabric-collector --feature-gates=service.AllowNoPipelines --config collector.yaml
```

**Flags and subcommands** (provided by the upstream `otelcol` command):

| Flag / subcommand | Default | Description |
|---|---|---|
| `--config URI` | — | Config location; repeat it to merge several. A bare path, or a `file:`, `env:` or `yaml:` URI |
| `--set KEY=VALUE` | — | Override one property of a component that the config defines |
| `--feature-gates LIST` | upstream defaults | Comma-separated feature gates; prefix `-` to disable |
| `-v`, `--version` | — | Print the version |
| `-h`, `--help` | — | Print help |
| `components` | — | List the components built in |
| `validate --config URI` | — | Check a config without running it (exits `1` for any pipeline, as above) |
| `print-config`, `featuregate`, `completion` | — | Upstream helpers |

**Exit codes:** `0` on success (help, version, components, a clean shutdown); `1` on any
error, including a config that fails to load or validate.

**`service::telemetry` options:**

| Option | Default | Effect |
|---|---|---|
| `logs::level` | `info` | `debug`, `info`, `warn` or `error`. Any other value fails `validate` |

Any other key under `service::telemetry` (for example `metrics`) is rejected when the
config loads.

**`novaseal_batch_signer` options** (`processors:` section of the config):

| Option | Default | Effect |
|---|---|---|
| `local_wal` | `false` | Sign with the local dev key (see [The dev key](#the-dev-key)). The only signer that works today |
| `fail_open` | `false` | `false`: with no signer the processor refuses to start, and a batch that fails to sign is dropped. `true`: batches go out unsigned, with a warning at start |
| `keystore_endpoint` | — | **planned**, not usable: a set value makes the processor refuse to start, because the KMS keystore needs an mTLS client certificate the config has no fields for |
| `timeout` | `5s` | Deadline for signing one batch |
| `send_batch_size` | `1000` | Accepted, no effect yet |
| `cache_ttl` | `5m` | Accepted, no effect yet |
| `key_rotation_interval` | `1h` | Accepted, no effect yet |

The processor drops log records whose JSON body carries an `envelope_version` other than
`"1"`, then writes the signature into the resource attributes `nova.batch.signature`
(base64 Ed25519) and `nova.batch.signing_key_id`.

**Metrics.** The processor records `nova_batch_sign_latency_seconds` and
`nova_invalid_envelope_version_total` in the Go Prometheus default registry
(`collector/pkg/metrics`). No binary serves that registry over HTTP today, and
`nova_batch_sign_errors_total` and `nova_collector_forwarded_events_total` are defined
but not incremented by any binary.

**Environment.** `NOVAFABRIC_ENV=production` makes `local_wal: true` fail at start.
`HOME` locates the dev key. `${NAME}` (or `${env:NAME}`) in a config expands from the
environment when the config loads.

---

## novafabric-verifier

**experimental.** Verifies the NovaSeal Ed25519 signature on one OTLP `ResourceLogs`
batch, offline: no network and no KMS. Source: `collector/cmd/novafabric-verifier`.

```bash
novafabric-verifier --batch batch.pb --pubkey signer-public.pem --key-id <key-id>
novafabric-verifier --batch batch.pb --local-wal
```

| Flag | Default | Description |
|---|---|---|
| `--batch FILE` | — (required) | One serialized OTLP `ResourceLogs` protobuf message (not a `LogsData` or export request) that carries the `nova.batch.signature` and `nova.batch.signing_key_id` resource attributes |
| `--pubkey FILE` | — | PEM Ed25519 public key: the standard SPKI `PUBLIC KEY` encoding, or a raw 32-byte body. Needs `--key-id` |
| `--key-id ID` | — | Key ID the batch must carry in `nova.batch.signing_key_id`; required with `--pubkey` |
| `--local-wal` | `false` | Verify with the local dev key instead (see [The dev key](#the-dev-key)). Wins if `--pubkey` is also given |

One of `--pubkey` or `--local-wal` is required.

**How it checks.** It removes the two signing attributes, sorts the resource and
log-record attributes by key, marshals the message with deterministic protobuf encoding,
and checks the Ed25519 signature over those bytes. A batch altered after signing, a
missing attribute, a wrong key, or a key ID that differs from the one in the batch all
give `INVALID`.

**Output** (stdout):

```text
OK key_id=<id>
INVALID key_id=<id> error=<reason>
```

**Exit codes.**

| Code | Meaning |
|---|---|
| `0` | The signature verifies |
| `1` | It does not, or the batch or key file could not be read or parsed |
| `2` | Usage error: `--batch` missing, neither `--pubkey` nor `--local-wal`, `--pubkey` without `--key-id`, an unknown flag, or `-h` / `--help` |

**Environment.** `HOME` locates the dev key for `--local-wal`;
`NOVAFABRIC_ENV=production` makes `--local-wal` refuse to run (exit `1`).

### The dev key

`--local-wal` (verifier) and `local_wal: true` (processor) use the same local key store,
`collector/pkg/novaseal/keystore_local_wal.go`. It is meant for development only.

- The key lives in `$HOME/.novafabric/dev-keys/<key-id>.pem` (directory mode 0700, file
  mode 0400). Exactly one `*.pem` file may be there; with more than one, both tools fail.
- If the directory holds no key, **a new one is generated**. For the verifier that means
  the check runs against a fresh key and prints `INVALID`. Verify on the machine, and
  under the same `HOME`, that signed the batch.
- Every use logs `USING LOCAL DEV KEY — NOT FOR PRODUCTION`.

---

## libnovaspool

**experimental.** A C shared library (`go build -buildmode=c-shared`, source
`collector/pkg/cffi/exports.go`) that writes spool segments in the same format as the Go
spool, so a C or Python program can feed the forwarder without running a Go process. It
is not shipped in any package; build it yourself (see [Building](#building)).

| C function | Returns | Description |
|---|---|---|
| `int SpoolWrite(const char *spool_dir, const char *event_json, int event_len)` | `0` / `-1` | Commits one event as a new `<seq>.jsonl` segment. The directory is created if missing. The sequence starts above every committed segment and the forwarder's checkpoint, and the commit uses `link(2)`, so two writers never replace each other's segment |
| `int SpoolDrain(const char *spool_dir)` | `0` | No-op: each `SpoolWrite` is already durable when it returns |
| `char *SpoolLastError(void)` | string | The last error message, in a newly allocated C string the caller must `free()`. Not safe to call from several threads |

`SpoolWrite` enforces no size or segment-count bound. The pure-Python `NovaPySpool`
writer does (`max_files`, default 10 000, raises `SpoolFullError`); with the library
loaded, that bound is not applied.

**Python loader** (`novafabric.collector_cffi.spool`). It looks for the library at
`$NOVA_LIBSPOOL_PATH`, then `<repo>/lib/libnovaspool.so`, then on the dynamic linker
path. If none loads, `NovaPySpool` falls back to the pure-Python writer.

---

## OCB build config

**future design.** `collector/ocb/builder-config.yaml` is an OpenTelemetry Collector
Builder config for a fuller distribution: the `otlp` receiver, the `batch` processor, the
`debug` and `otlp` exporters, the `zpages` extension, and the NovaSeal processor, written
to `./bin/novafabric-collector`. It has no `kafka` exporter.

The file exists, but no working build comes from it today:

- CI does not run the builder.
- The NovaSeal entry names the module root `github.com/MSKazemi/novafabric/collector`,
  which has no Go package. The processor lives in
  `collector/internal/processor/novasealbatchsigner`, and Go does not let another module
  import an `internal/` package, so the builder's generated code cannot reach it as
  written.
- It pins `otelcol_version: "0.104.0"`, while `collector/go.mod` requires
  `go.opentelemetry.io/collector/otelcol v0.165.0`.

## See also

- [CLI reference: collector binaries](cli-reference.md#collector-binaries-phase-2--cluster-scale):
  the `nova`-side surfaces and `novafabric-hpc-hub`.
- [Warm capture daemon](warm-capture-daemon.md): where `--emit-spool` and the forwarder
  sit in the resident-emitter work.
- [Developer guide](developer-guide.md): building and testing the Go module.
