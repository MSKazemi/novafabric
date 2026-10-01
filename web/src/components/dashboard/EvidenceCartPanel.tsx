/**
 * EvidenceCartPanel — ADR-0239 (experimental): review the session cart and
 * export it as **one** signed Evidence Bundle.
 *
 * Renders nothing while the cart is empty, so a user who never adds to it sees
 * no change (spec acceptance criterion 1). Export is `admin`-scoped and
 * audited server-side; this panel's job is to make every outcome legible:
 *
 * - **progress** — a busy state while the server resolves and signs;
 * - **unresolved items** (409) — listed per item with the server's reason, and
 *   the only way forward is an explicit "export and record the omissions";
 * - **refusals** — 403 (needs admin), 413 (over the stated bound), 422,
 *   429 (another export running), 503 (audit unavailable, nothing written) —
 *   shown with the server's own message, never collapsed to "failed".
 */
import { useState } from 'react';
import { clsx } from 'clsx';
import { api, type CartExportFailure, type CartExportOk } from '../../lib/api';
import { MAX_CART_ITEMS, useEvidenceCart } from '../../lib/evidenceCart';

type Phase =
  | { kind: 'idle' }
  | { kind: 'confirm' }
  | { kind: 'busy' }
  | { kind: 'done'; result: CartExportOk }
  | { kind: 'failed'; failure: CartExportFailure };

const STATUS_HINT: Record<number, string> = {
  403: 'Export needs an admin-scoped credential (ADR-0228).',
  413: 'The cart is over the export bound — split it, or export from the CLI.',
  422: 'The server could not build a bundle from this cart.',
  429: 'Another cart export is running on this server — retry when it finishes.',
  503: 'The audit entry could not be written, so no bundle was kept.',
};

export default function EvidenceCartPanel({ onFlash }: {
  onFlash?: (tone: 'success' | 'error', text: string) => void;
}) {
  const cart = useEvidenceCart();
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' });
  const [open, setOpen] = useState(false);

  if (cart.items.length === 0 && phase.kind !== 'done') return null;

  const over = cart.items.length > MAX_CART_ITEMS;

  const runExport = async (acceptUnresolved: boolean) => {
    setPhase({ kind: 'busy' });
    try {
      const res = await api.exportEvidenceCart(cart.items, { accept_unresolved: acceptUnresolved });
      if (res.ok) {
        setPhase({ kind: 'done', result: res });
        onFlash?.('success', `Cart bundle written: ${res.bundle_path}`);
      } else {
        setPhase({ kind: 'failed', failure: res });
      }
    } catch (e) {
      setPhase({
        kind: 'failed',
        failure: {
          ok: false, status: 0, error: null, message: (e as Error).message, remedy: null, unresolved: [],
        },
      });
    }
  };

  return (
    <section
      aria-label="Evidence cart"
      data-testid="evidence-cart"
      className="lg:col-span-2 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] px-3 py-2 text-xs"
    >
      <div className="flex items-center gap-2 flex-wrap">
        <button
          type="button"
          onClick={() => setOpen(o => !o)}
          aria-expanded={open}
          className="font-mono uppercase tracking-wider text-[10px] text-[var(--color-text-muted)] hover:text-[var(--color-text)]"
        >
          {open ? '▾' : '▸'} Evidence cart ({cart.items.length})
        </button>
        <span className="text-[10px] text-[var(--color-text-faint)]">
          experimental · references only, resolved once at export · cleared when this browser session ends
        </span>
        <span className="ml-auto flex items-center gap-1.5">
          <button
            type="button"
            onClick={() => { cart.clear(); setPhase({ kind: 'idle' }); }}
            disabled={phase.kind === 'busy' || cart.items.length === 0}
            className="px-2 py-0.5 rounded border border-[var(--color-border)] text-[10px] text-[var(--color-text-muted)] hover:text-[var(--color-text)] disabled:opacity-40"
          >Clear</button>
          <button
            type="button"
            data-testid="cart-export"
            onClick={() => setPhase({ kind: 'confirm' })}
            disabled={phase.kind === 'busy' || cart.items.length === 0 || over}
            title={over ? `At most ${MAX_CART_ITEMS} items per export` : 'Export the cart as one signed Evidence Bundle'}
            className={clsx(
              'px-2 py-0.5 rounded border text-[10px] font-medium',
              'border-[var(--color-accent)] text-[var(--color-accent)] hover:bg-[var(--color-accent)] hover:text-white',
              'disabled:opacity-40 disabled:cursor-not-allowed',
            )}
          >{phase.kind === 'busy' ? 'Exporting…' : 'Export cart'}</button>
        </span>
      </div>

      {over && (
        <p role="alert" className="mt-1 text-[var(--color-status-failure)]">
          {cart.items.length} items — one export is bounded at {MAX_CART_ITEMS}. Remove some items first.
        </p>
      )}

      {open && cart.items.length > 0 && (
        <ol className="mt-2 space-y-1">
          {cart.items.map(item => (
            <li key={`${item.kind}:${item.ref}`} className="flex items-center gap-2 font-mono text-[11px]">
              <span className="text-[var(--color-text-faint)] w-14 shrink-0">{item.kind}</span>
              <span className="truncate" title={item.ref}>{item.ref}</span>
              <span className="text-[10px] text-[var(--color-text-faint)] shrink-0">{item.added_at.slice(0, 19)}</span>
              <button
                type="button"
                onClick={() => cart.remove(item.kind, item.ref)}
                aria-label={`Remove ${item.ref} from the cart`}
                className="ml-auto text-[var(--color-text-faint)] hover:text-[var(--color-status-failure)]"
              >×</button>
            </li>
          ))}
        </ol>
      )}

      {phase.kind === 'confirm' && (
        <div role="dialog" aria-label="Confirm cart export" className="mt-2 rounded border border-[var(--color-border)] p-2 space-y-1">
          <p>
            Export {cart.items.length} reference{cart.items.length === 1 ? '' : 's'} as one signed Evidence Bundle.
            The bundle states it is an <strong>operator-curated subset</strong>, discloses any legal holds,
            and the export is recorded in the hash-chained audit log. Requires an <code>admin</code> credential.
          </p>
          <div className="flex gap-2">
            <button type="button" onClick={() => runExport(false)} className="px-2 py-0.5 rounded border border-[var(--color-accent)] text-[var(--color-accent)]">Export</button>
            <button type="button" onClick={() => setPhase({ kind: 'idle' })} className="px-2 py-0.5 rounded border border-[var(--color-border)]">Cancel</button>
          </div>
        </div>
      )}

      {phase.kind === 'busy' && (
        <p role="status" aria-live="polite" className="mt-2 text-[var(--color-text-muted)]">
          Resolving references and signing the bundle…
        </p>
      )}

      {phase.kind === 'done' && (
        <div role="status" data-testid="cart-export-done" className="mt-2 space-y-0.5 text-[var(--color-status-success)]">
          <p>Bundle written: <code className="font-mono">{phase.result.bundle_path}</code></p>
          <p className="text-[var(--color-text-muted)] font-mono text-[10px]">
            sha256 {phase.result.bundle_sha256.slice(0, 16)}… · {phase.result.capsule_count} capsules · audit {phase.result.audit_entry_hash.slice(0, 12)}…
            {phase.result.contains_held_evidence && ' · contains held evidence'}
            {phase.result.unresolved.length > 0 && ` · ${phase.result.unresolved.length} omission(s) recorded`}
          </p>
          <p className="text-[var(--color-text-muted)]">Check it: <code className="font-mono">{phase.result.cli_verify}</code></p>
        </div>
      )}

      {phase.kind === 'failed' && (
        <div role="alert" data-testid="cart-export-error" className="mt-2 space-y-1 text-[var(--color-status-failure)]">
          <p>
            {phase.failure.error === 'unresolved_items'
              ? `${phase.failure.unresolved.length} reference(s) could not be resolved — nothing was exported.`
              : phase.failure.message}
          </p>
          {STATUS_HINT[phase.failure.status] && phase.failure.error !== 'unresolved_items' && (
            <p className="text-[var(--color-text-muted)]">{STATUS_HINT[phase.failure.status]}</p>
          )}
          {phase.failure.unresolved.length > 0 && (
            <>
              <ul className="font-mono text-[11px] text-[var(--color-text-muted)]">
                {phase.failure.unresolved.map(u => (
                  <li key={`${u.kind}:${u.ref}`}>{u.kind} {u.ref} — {u.unresolved_reason ?? 'unresolved'}</li>
                ))}
              </ul>
              <button
                type="button"
                onClick={() => runExport(true)}
                className="px-2 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text)]"
              >Export anyway and record the omissions</button>
            </>
          )}
          {phase.failure.remedy && phase.failure.error !== 'unresolved_items' && (
            <p className="text-[var(--color-text-muted)]">{phase.failure.remedy}</p>
          )}
        </div>
      )}
    </section>
  );
}
