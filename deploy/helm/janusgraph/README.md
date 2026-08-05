# janusgraph Helm Chart

Minimal Helm chart for deploying JanusGraph 1.1.0 as the NovaFabric lineage
v3 tier (distributed graph DB).

## Quick start

```bash
helm install janusgraph ./deploy/helm/janusgraph \
  --namespace novafabric \
  --create-namespace
```

Connect with the JanusGraphLineageStore:

```python
from novafabric.lineage.backends.janusgraph import JanusGraphLineageStore

store = JanusGraphLineageStore(
    gremlin_endpoint="ws://janusgraph.novafabric.svc.cluster.local:8182/gremlin"
)
```

## Configuration

| Parameter | Default | Description |
|---|---|---|
| `replicaCount` | `1` | Number of JanusGraph replicas |
| `image.tag` | `1.1.0` | JanusGraph Docker image tag |
| `gremlinPort` | `8182` | Gremlin WebSocket port |
| `service.type` | `ClusterIP` | Kubernetes service type |
| `persistence.size` | `10Gi` | PVC size for graph data |
| `config.storageBackend` | `berkeleyje` | JanusGraph storage backend (`berkeleyje` for single-node; `cassandra` or `hbase` for cluster) |
| `config.indexSearchBackend` | `lucene` | Index backend (`lucene` for single-node; `elasticsearch` for cluster) |
| `resources.requests.memory` | `1Gi` | JVM heap request |
| `resources.limits.memory` | `4Gi` | JVM heap limit |

## Cluster-scale deployment

For production multi-node deployments, override the storage and index backends:

```yaml
config:
  storageBackend: cassandra
  indexSearchBackend: elasticsearch
```

This chart deploys single-node (berkeleyje + lucene) by default, which is
suitable for development and integration testing only.

## Notes

- This chart is tagged as **v3 tier** in the lineage backend tiering (ADR-0053).
- The embedded berkeleyje backend does not support horizontal scaling.
- JanusGraph image: `janusgraph/janusgraph:1.1.0` (Apache-2.0).
- gremlinpython extra: `uv add 'novafabric[janusgraph]'`
