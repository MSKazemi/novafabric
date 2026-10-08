// Forward-secure per-node signing key ratchet (ADR-0089). Extracted verbatim
// from SealTab.tsx (dashboard-modernization split).
import { useState } from 'react';
import { api } from '../../../../lib/api';
import ActionButton from '../../../ui/ActionButton';
import { useMutation } from '../../../../lib/useMutation';

/** Forward-secure per-node signing key ratchet (ADR-0089). */
export default function RatchetPanel() {
  const [nodeId, setNodeId] = useState('');
  const [state, setState] = useState<Record<string, unknown> | null>(null);
  const [epochs, setEpochs] = useState<number[]>([]);

  const status = useMutation((id: string) => api.ratchetStatus(id), {
    silentSuccess: true,
    onSuccess: (r) => { setState(r.state ?? null); setEpochs(r.registry_epochs ?? []); },
  });
  const init = useMutation((id: string) => api.ratchetInit(id), {
    successMessage: 'Ratchet initialised', onSuccess: (r) => setState(r.state ?? null),
  });
  const rotate = useMutation((id: string) => api.ratchetRotate(id), {
    successMessage: 'Rotated to next epoch', onSuccess: (r) => setState(r.state ?? null),
  });

  const id = nodeId.trim();
  return (
    <div className="rounded border border-[var(--color-border)] p-4 space-y-2">
      <h3 className="text-sm font-medium text-[var(--color-text)]">Signing key ratchet <span className="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)]">ADR-0089</span></h3>
      <p className="text-xs text-[var(--color-text-muted)]">Forward-secure per-node epochs. Rotation erases the previous chain key (best-effort).</p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="flex flex-col gap-1 text-[10px] text-[var(--color-text-faint)]">
          Node ID
          <input value={nodeId} onChange={(e) => setNodeId(e.target.value)} placeholder="node-a" className="w-48 px-2 py-1 text-xs font-mono rounded border border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text)]" />
        </label>
        <ActionButton onClick={() => status.run(id)} pending={status.pending} disabled={!id}>Status</ActionButton>
        <ActionButton onClick={() => init.run(id)} pending={init.pending} disabled={!id} variant="primary">Init epoch 0</ActionButton>
        <ActionButton
          onClick={() => rotate.run(id)}
          pending={rotate.pending}
          disabled={!id}
          variant="danger"
          confirm={{ title: 'Rotate signing epoch?', body: 'Advances to the next epoch and erases the previous chain key (irreversible).', confirmLabel: 'Rotate', tone: 'danger' }}
        >
          Rotate
        </ActionButton>
      </div>
      {state && (
        <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] p-3 text-xs font-mono space-y-1">
          <div>node: {String(state.node_id ?? '—')}</div>
          <div>epoch: {String(state.epoch ?? '—')}</div>
          <div>rotated_at: {String(state.rotated_at ?? '—')}</div>
          {epochs.length > 0 && <div className="text-[var(--color-text-faint)]">registry epochs: [{epochs.join(', ')}]</div>}
        </div>
      )}
    </div>
  );
}
