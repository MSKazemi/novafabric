/**
 * Browser-side ed25519 signature verification using SubtleCrypto.
 * Modern browsers (Chrome 113+, Firefox 130+, Safari 17+) support Ed25519
 * natively in WebCrypto — no extra deps needed.
 */

import { evidenceBundle } from './fixtures';

interface DsseSignature {
  keyid: string;
  alg: string;
  sig_b64: string;
  message_hash_sha256: string;
  canonical_message_length: number;
}

export interface VerifyResult {
  ok: boolean;
  error?: string;
  durationMs: number;
  keyid: string;
  signatureFingerprint: string;
  canonicalMessageBytes: number;
  messageHash: string;
}

function pemToArrayBuffer(pem: string): ArrayBuffer {
  const b64 = pem.replace(/-----[A-Z ]+-----/g, '').replace(/\s+/g, '');
  const binary = atob(b64);
  const buf = new ArrayBuffer(binary.length);
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return buf;
}

function base64ToBuffer(b64: string): ArrayBuffer {
  const binary = atob(b64);
  const buf = new ArrayBuffer(binary.length);
  const bytes = new Uint8Array(buf);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return buf;
}

function strToBuffer(s: string): ArrayBuffer {
  const encoded = new TextEncoder().encode(s);
  const buf = new ArrayBuffer(encoded.length);
  new Uint8Array(buf).set(encoded);
  return buf;
}

function canonicalize(obj: unknown): string {
  if (obj === null || typeof obj !== 'object') return JSON.stringify(obj);
  if (Array.isArray(obj)) return '[' + obj.map(canonicalize).join(',') + ']';
  const o = obj as Record<string, unknown>;
  const keys = Object.keys(o).sort();
  return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonicalize(o[k])).join(',') + '}';
}

async function sha256Hex(buf: ArrayBuffer): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', buf);
  return Array.from(new Uint8Array(hash)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

let cachedKey: CryptoKey | null = null;
async function getPublicKey(): Promise<CryptoKey> {
  if (cachedKey) return cachedKey;
  cachedKey = await crypto.subtle.importKey(
    'spki',
    pemToArrayBuffer(evidenceBundle.publicKeyPem),
    { name: 'Ed25519' },
    false,
    ['verify'],
  );
  return cachedKey;
}

export async function verifyEvidenceBundle(tampered: boolean): Promise<VerifyResult> {
  const t0 = performance.now();
  const dsse = evidenceBundle.dsse as unknown as { signature: DsseSignature };
  const sig = dsse.signature;
  const sigBuf = base64ToBuffer(sig.sig_b64);

  // Reconstruct the canonical message from the manifest. If tampered, mutate one byte.
  const manifest = JSON.parse(JSON.stringify(evidenceBundle.manifest));
  if (tampered) {
    if (manifest.files && manifest.files.length > 0) {
      const original = manifest.files[0].sha256 as string;
      manifest.files[0].sha256 = original.slice(0, -1) + (original.endsWith('a') ? 'b' : 'a');
    }
  }

  const canonical = canonicalize(manifest);
  const messageBuf = strToBuffer(canonical);
  const messageHash = await sha256Hex(messageBuf);

  let ok = false;
  let error: string | undefined;
  try {
    const key = await getPublicKey();
    ok = await crypto.subtle.verify('Ed25519', key, sigBuf, messageBuf);
  } catch (e) {
    error = (e as Error).message;
  }

  const durationMs = performance.now() - t0;
  return {
    ok,
    error,
    durationMs,
    keyid: sig.keyid,
    signatureFingerprint: sig.sig_b64.slice(0, 16) + '…',
    canonicalMessageBytes: messageBuf.byteLength,
    messageHash,
  };
}

export function isSubtleEd25519Supported(): boolean {
  if (typeof crypto === 'undefined' || !crypto.subtle) return false;
  return true;
}
