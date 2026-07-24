/**
 * Local API client for the generic compliance-export registry (ADR-0200 §2).
 *
 * Deliberately NOT part of web/src/lib/api.ts: the registry is server-driven
 * (`GET /api/compliance/export/kinds` describes every kind and its fields),
 * so there is no per-kind TypeScript surface to keep in sync — this thin
 * module only speaks the two generic endpoints. It reuses the same
 * connection/token convention as the shared client (token as ?token=…).
 */

import { getConnection, ServeApiError } from '../../../lib/api';

export interface ExportFieldSpec {
  key: string;
  label: string;
  type: 'string' | 'boolean' | 'json';
  required: boolean;
}

export interface ExportKindSpec {
  kind: string;
  label: string;
  cli_equivalent: string;
  fields: ExportFieldSpec[];
  output: 'document' | 'zip';
  note: string;
}

export interface ExportRunResult {
  ok: boolean;
  kind: string;
  run_id: string | null;
  document?: Record<string, unknown>;
  zip_base64?: string;
  filename?: string;
  cli_equivalent: string;
  note: string;
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const { token, base } = getConnection();
  if (!token) throw new ServeApiError(401, 'no token configured — connect first');
  const params = new URLSearchParams({ token });
  const res = await fetch(`${base}${path}?${params.toString()}`, init);
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body && typeof body === 'object' && 'detail' in body) message = String(body.detail);
    } catch {
      /* ignore */
    }
    throw new ServeApiError(res.status, message);
  }
  return res.json() as Promise<T>;
}

export function fetchExportKinds(): Promise<{ kinds: ExportKindSpec[]; count: number }> {
  return requestJson('/api/compliance/export/kinds');
}

export function runExport(
  kind: string,
  body: Record<string, unknown>,
): Promise<ExportRunResult> {
  return requestJson(`/api/compliance/export/${kind}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}
