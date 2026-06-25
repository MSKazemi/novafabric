# NovaFabric Kubernetes Deployment Guide

This directory contains manifests for deploying the NovaFabric collector tier on Kubernetes.

## Status

**planned** — The Kubernetes collector profile is Phase 2 of the NovaFabric cluster-scale
roadmap. The `ghcr.io/novafabric/novafabric-collector:latest` image is not yet published;
see `collector/` for the Go module build target.

---

## 1. Prerequisites

| Component | Minimum version | Notes |
|---|---|---|
| Kubernetes | 1.28 | Any distribution (EKS, GKE, AKS, kubeadm) |
| `kubectl` | 1.28 | Matching server version |
| Kafka | 3.6+ | External; accessible from the cluster |
| NovaSeal KMS | 0.1.0 | `src/novafabric/trust/novaseal/` |
| Fluent Bit | 3.0 | Included as DaemonSet; image pulled from `fluent/fluent-bit:3.0` |

---

## 2. Apply Order

Apply manifests in this order to satisfy dependencies:

```bash
# 1. Create namespace
kubectl apply -f namespace.yaml

# 2. Create secrets (edit the example first — never apply the .example file directly)
cp secret-novaseal-mtls.yaml.example secret-novaseal-mtls.yaml
# Edit secret-novaseal-mtls.yaml with real base64-encoded certificates
kubectl apply -f secret-novaseal-mtls.yaml

# 3. Create ConfigMap
kubectl apply -f configmap-collector.yaml

# 4. Deploy gateway collector
kubectl apply -f deployment-gateway-collector.yaml

# 5. Expose gateway collector service
kubectl apply -f service.yaml

# 6. Deploy node-side log collector
kubectl apply -f daemonset-fluent-bit.yaml
```

---

## 3. Environment Variables

These environment variables must be provided via Kubernetes Secrets or a ConfigMap.
The Deployment references `novafabric-collector-secrets` by name.

| Variable | Description | Required |
|---|---|---|
| `NOVASEAL_KMS_ENDPOINT` | NovaSeal KMS gRPC endpoint for batch signing | Yes |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka bootstrap server(s) (`host:port,host:port`) | Yes |
| `NOVAFABRIC_CLUSTER_ID` | Cluster identifier (used in Kafka topic name) | Recommended |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP endpoint for Fluent Bit output (injected into DaemonSet) | Recommended |

Create the required secret:

```bash
kubectl create secret generic novafabric-collector-secrets \
  --namespace=novafabric \
  --from-literal=novaseal-kms-endpoint=grpc://novaseal:50051 \
  --from-literal=kafka-bootstrap-servers=kafka:9092
```

---

## 4. Scaling Notes

The gateway collector Deployment defaults to 2 replicas. Scale horizontally for
higher throughput:

```bash
kubectl scale deployment novafabric-gateway-collector \
  --namespace=novafabric \
  --replicas=4
```

Resource limits per replica (see `deployment-gateway-collector.yaml`):
- CPU: 100m request / 500m limit
- Memory: 128Mi request / 512Mi limit

At 100K events/sec cluster-wide, 2 replicas with default batch settings (1000 events,
5s timeout) are sufficient for initial validation. See ADR-0043 for performance targets.

---

## 5. Spool Directory

The Fluent Bit DaemonSet reads from `/var/lib/novafabric/spool/**/*.jsonl` on each node.
This path must be created by the agent SDK before events are written:

```python
# In the novafabric Python SDK (local mode):
# NOVAFABRIC_SPOOL_DIR=/var/lib/novafabric/spool/<run_id>/events.jsonl
```

The hostPath mount assumes the agent runs as a Pod on the same node. For sidecar
injection patterns, mount the spool as an emptyDir shared between the agent
container and the Fluent Bit sidecar.
