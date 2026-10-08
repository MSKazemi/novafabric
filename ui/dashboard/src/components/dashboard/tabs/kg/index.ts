/**
 * KG tab panels — one file per cohesive panel, extracted verbatim from the
 * former KGTab.tsx monolith (behavior frozen).
 */
export {
  inputClass,
  labelClass,
  type KGEdge,
  type KGStatus,
  type KGTopology,
  type MCPServerEdge,
  type ModelEdge,
  type ToolEdge,
} from './shared';
export { default as StatusPanel } from './StatusPanel';
export { default as AgentQueryPanel } from './AgentQueryPanel';
export { default as TopologyLayerPanel } from './TopologyLayerPanel';
export { default as KGInitPanel } from './KGInitPanel';
export { default as KGIngestPanel } from './KGIngestPanel';
export { default as KGIngestAllPanel } from './KGIngestAllPanel';
export { default as KGQueryPanel } from './KGQueryPanel';
export { default as KGAuditPanel } from './KGAuditPanel';
export { default as EntityQueuePanel } from './EntityQueuePanel';
export { default as KGAliasPanel } from './KGAliasPanel';
