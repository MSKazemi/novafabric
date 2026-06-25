import { useEffect, useState, useCallback } from 'react';
import { api } from '../../../lib/api';
import CopyButton from '../../ui/CopyButton';
import { ErrorBox } from '../helpers';
import { SkeletonRows } from '../../ui/Skeleton';

// DB-SCH-1 — Schema inspector tab (v0.19.0, cap-001 / ADR-0066)

interface SchemaInfo {
  schema_version: string;
  spec_path: string;
  event_types: string[];
  event_type_count: number;
  categories: Record<string, string[]>;
  pydantic_models: Array<{ name: string; fields: string[]; description: string }>;
  note: string;
}

const labelClass =
  'text-[10px] font-mono uppercase tracking-wider text-[var(--color-text-faint)]';

function CategoryGrid({ categories }: { categories: Record<string, string[]> }) {
  const entries = Object.entries(categories);
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
      {entries.map(([cat, events]) => (
        <div key={cat} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 space-y-1.5">
          <div className="flex items-center justify-between">
            <p className="text-[11px] font-semibold text-[var(--color-text)]">{cat}</p>
            <span className="text-[9px] font-mono text-[var(--color-text-faint)]">{events.length}</span>
          </div>
          <ul className="space-y-0.5">
            {events.map((e) => (
              <li key={e} className="text-[10px] font-mono text-[var(--color-text-muted)] pl-2 border-l border-[var(--color-border)]">
                {e}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function PydanticModels({ models }: { models: SchemaInfo['pydantic_models'] }) {
  return (
    <div className="space-y-2">
      {models.map((m) => (
        <div key={m.name} className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 space-y-1.5">
          <div className="flex items-center justify-between">
            <p className="text-[11px] font-mono font-semibold text-[var(--color-text)]">{m.name}</p>
            <span className="text-[9px] font-mono text-[var(--color-text-faint)]">{m.fields.length} fields</span>
          </div>
          <p className="text-[10px] text-[var(--color-text-muted)]">{m.description}</p>
          <div className="flex flex-wrap gap-1">
            {m.fields.map((f) => (
              <code key={f} className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[var(--color-bg-raised)] border border-[var(--color-border)] text-[var(--color-text-muted)]">
                {f}
              </code>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

export default function SchemaTab() {
  const [schema, setSchema] = useState<SchemaInfo | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    setLoading(true);
    setErr(null);
    api.schemaList()
      .then(setSchema)
      .catch((e) => setErr((e as Error).message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-sm font-semibold text-[var(--color-text)]">Capsule Event Schema</h2>
          <p className="text-[10px] text-[var(--color-text-faint)] mt-0.5">
            The 25 canonical event types + Pydantic models (cap-001 · ADR-0066)
          </p>
        </div>
        <div className="flex gap-1.5">
          {(['cap-001', 'ADR-0066', 'v0.19.0'] as const).map((label) => (
            <span
              key={label}
              className="text-[9px] font-mono uppercase tracking-wider px-1.5 py-0.5 rounded border border-[var(--color-border)] text-[var(--color-text-faint)]"
            >
              {label}
            </span>
          ))}
        </div>
      </div>

      {err && <ErrorBox message={err} onRetry={load} />}

      {loading && !schema && !err && <SkeletonRows rows={6} />}

      {schema && (
        <>
          <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
                <p className={labelClass}>schema_version</p>
                <p className="text-base font-mono font-bold text-[var(--color-text)]">{schema.schema_version}</p>
              </div>
              <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2">
                <p className={labelClass}>event types</p>
                <p className="text-base font-mono font-bold text-[var(--color-text)]">{schema.event_type_count}</p>
              </div>
              <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-sunken)] px-3 py-2 relative">
                <p className={labelClass}>spec path</p>
                <p className="text-[11px] font-mono text-[var(--color-text-muted)] truncate pr-8">{schema.spec_path}</p>
                <div className="absolute top-1.5 right-1.5">
                  <CopyButton text={schema.spec_path} label="path" />
                </div>
              </div>
            </div>
          </section>

          <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
            <h3 className="text-xs font-semibold text-[var(--color-text)]">Event Types by Category</h3>
            <CategoryGrid categories={schema.categories} />
          </section>

          <section className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-raised)] p-4 space-y-3">
            <h3 className="text-xs font-semibold text-[var(--color-text)]">Pydantic Models</h3>
            <PydanticModels models={schema.pydantic_models} />
          </section>

          <p className="text-[10px] text-[var(--color-text-faint)]">{schema.note}</p>
        </>
      )}
    </div>
  );
}
