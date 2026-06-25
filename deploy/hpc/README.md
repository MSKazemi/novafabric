# NovaFabric HPC Deployment Guide

This directory contains Slurm Prolog/Epilog scripts, NATS configuration templates,
and an Ansible playbook for deploying the NovaFabric collector tier on HPC clusters.

## Status

**planned** — The HPC collector (Slurm + NATS profile) is Phase 2 of the NovaFabric
cluster-scale roadmap. The scripts and configuration templates in this directory are
reference artifacts for implementers. The `novafabric-hpc-hub` binary is not yet
shipped; see `collector/` for the Go module build target.

---

## 1. Prerequisites

| Component | Minimum version | Notes |
|---|---|---|
| Slurm | 22.05 | Required for `PrologFlags=Alloc` |
| NATS Server | 2.10 | JetStream enabled; leaf node support |
| `novafabric-hpc-hub` | 0.1.0 | Built from `collector/cmd/novafabric-hpc-hub` |
| `nats` CLI | 0.1.0 | Used by epilog for stream flush |

---

## 2. Installation

### 2a. Copy scripts to compute nodes (manual)

```bash
cp prolog.sh epilog.sh /opt/novafabric/bin/
chmod 755 /opt/novafabric/bin/prolog.sh /opt/novafabric/bin/epilog.sh
```

Or use the Ansible playbook: `ansible-playbook -i inventory.example.ini install.yml`

### 2b. Configure slurm.conf

Add the following lines to `/etc/slurm/slurm.conf` on the Slurm controller:

```
Prolog=/opt/novafabric/bin/prolog.sh
Epilog=/opt/novafabric/bin/epilog.sh
PrologFlags=Alloc
```

Reload Slurm after editing:

```bash
scontrol reconfigure
```

### 2c. Deploy NATS hub on head node

```bash
# Render the hub config template
envsubst < cluster-hub.conf.tmpl > /etc/nats/nova-hub.conf

# Start NATS hub
nats-server -c /etc/nats/nova-hub.conf &

# Create aggregate stream
nats stream add nova-${NOVAFABRIC_CLUSTER_ID}-aggregate \
  --subjects "nova.${NOVAFABRIC_CLUSTER_ID}.>" \
  --storage file \
  --retention limits \
  --max-age 24h \
  --replicas 1
```

### 2d. Start the forwarder

```bash
novafabric-forwarder --config forwarder.yaml
```

---

## 3. Environment Variables

| Variable | Default | Description |
|---|---|---|
| `NOVAFABRIC_SPOOL_BASE` | `/tmp/novafabric` | Base directory for per-job spools (local disk). |
| `NOVAFABRIC_HPC_STORAGE` | _(unset)_ | Set to `lustre` to use a Lustre-based spool path instead of local disk. |
| `LUSTRE_SPOOL_PATH` | _(unset)_ | Lustre mount point for spool; required when `NOVAFABRIC_HPC_STORAGE=lustre`. |
| `NOVAFABRIC_EPILOG_FLUSH_TIMEOUT` | `50` | Seconds to wait for NATS stream flush in epilog before proceeding. |
| `NOVAFABRIC_HUB_ADDRESS` | `localhost:4222` | NATS cluster hub address (`host:port`). |
| `NOVAFABRIC_CLUSTER_ID` | `local` | Cluster identifier string, embedded in NATS subjects and event envelopes. |
| `NOVASEAL_KMS_ENDPOINT` | _(required in prod)_ | NovaSeal KMS gRPC endpoint for batch signing. |
| `NOVAFABRIC_KMS_LOCAL_WAL` | `0` | Set to `1` for local dev mode (file-based WAL instead of KMS). **Dev-only; not for production.** |
| `NOVAFABRIC_ENV` | `hpc` | Runtime environment label written to `novafabric-job.conf`. |

---

## 4. Local Dev Mode

For development and integration testing without a real KMS, set:

```bash
export NOVAFABRIC_KMS_LOCAL_WAL=1
```

This enables a file-based signing WAL in the per-job spool directory.
**This mode is for development only and must not be used in production.**
The local WAL does not provide the key isolation, audit trail, or rotation
guarantees of a real KMS (see ADR-0041).

---

## 5. Slurm + Lustre Layout

When `NOVAFABRIC_HPC_STORAGE=lustre`, the per-job spool directory is:

```
${LUSTRE_SPOOL_PATH}/${SLURM_CLUSTER_NAME}/${SLURMD_NODENAME}/${SLURM_JOB_ID}/
```

The spool contains:
- `novafabric-job.conf` — per-job config written by prolog
- `hpc-hub.pid` — PID of the NATS leaf node process
- `hpc-hub.log` — leaf node stdout/stderr
- `epilog.log` — epilog flush and shutdown log
- `nats-store/` — JetStream file store (bounded to 1 GB; see `leaf-node.conf.tmpl`)

**Note:** The forwarder consumes from the hub stream, not from individual spool
directories. The spool is a local buffer; the hub stream is the durable source of truth.

---

## 6. Test Cluster

A 10-leaf + 1-hub + 1-mock-KMS + 1-Kafka reference deployment is available at
`deploy/hpc/test-cluster/docker-compose.yml`. Use it for integration testing:

```bash
cd deploy/hpc/test-cluster
docker compose up -d
```
