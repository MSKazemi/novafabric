/**
 * In-browser JSON-Schema validation. The schemas are the same files committed
 * under /schemas/ in the main repo — vendored into the bundle so the showcase
 * page proves the format is real, not marketing.
 */

import Ajv2020, { type ErrorObject } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { schemas } from './fixtures';

let cachedAjv: Ajv2020 | null = null;

function getAjv(): Ajv2020 {
  if (!cachedAjv) {
    cachedAjv = new Ajv2020({
      allErrors: true,
      strict: false,
      validateFormats: true,
    });
    addFormats(cachedAjv);
  }
  return cachedAjv;
}

export interface ValidationResult {
  ok: boolean;
  schemaVersion: string;
  errors?: Array<{ path: string; message: string; keyword: string }>;
  durationMs: number;
}

export function validateCapsule(data: unknown): ValidationResult {
  const ajv = getAjv();
  const schema = schemas.runCapsule as unknown as object;
  const validate = ajv.compile(schema);
  const t0 = performance.now();
  const ok = validate(data);
  const durationMs = performance.now() - t0;
  if (ok) {
    return { ok: true, schemaVersion: (schema as { $id?: string }).$id ?? 'run-capsule.schema.json', durationMs };
  }
  return {
    ok: false,
    schemaVersion: (schema as { $id?: string }).$id ?? 'run-capsule.schema.json',
    durationMs,
    errors: (validate.errors ?? []).map((e: ErrorObject) => ({
      path: e.instancePath || '/',
      message: e.message ?? 'unknown error',
      keyword: e.keyword,
    })),
  };
}

export function validateDiff(data: unknown): ValidationResult {
  const ajv = getAjv();
  const schema = schemas.diffReport as unknown as object;
  const validate = ajv.compile(schema);
  const t0 = performance.now();
  const ok = validate(data);
  const durationMs = performance.now() - t0;
  if (ok) {
    return { ok: true, schemaVersion: (schema as { $id?: string }).$id ?? 'diff-report.schema.json', durationMs };
  }
  return {
    ok: false,
    schemaVersion: (schema as { $id?: string }).$id ?? 'diff-report.schema.json',
    durationMs,
    errors: (validate.errors ?? []).map((e: ErrorObject) => ({
      path: e.instancePath || '/',
      message: e.message ?? 'unknown error',
      keyword: e.keyword,
    })),
  };
}
