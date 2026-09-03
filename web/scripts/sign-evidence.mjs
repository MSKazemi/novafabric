#!/usr/bin/env node
// Generate a deterministic ed25519 keypair (for showcase only — real evidence
// bundles use a per-deployment key per ADR-0011), sign the canonical manifest,
// and write the signed DSSE statement + public key into the fixture.
//
// Run via: npm run build:fixtures (after the Python script has emitted the
// unsigned manifest).

import { createHash, generateKeyPairSync, sign } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';

const ROOT = resolve(new URL('..', import.meta.url).pathname);
const BUNDLE = join(ROOT, 'src/data/fixtures/evidence-bundle');
const KEY_FILE = join(BUNDLE, 'demo-key.json');

const manifestPath = join(BUNDLE, 'manifest.json');
const dssePath = join(BUNDLE, 'dsse-statement.json');

if (!existsSync(manifestPath) || !existsSync(dssePath)) {
  console.error(`[error] Run \`npm run build:fixtures\` Python step first to generate ${manifestPath} and ${dssePath}`);
  process.exit(1);
}

// Reuse a previously-generated key if present (so re-running is idempotent and
// the committed pubkey matches the committed signature).
let privPem;
let pubPem;
if (existsSync(KEY_FILE)) {
  const stored = JSON.parse(readFileSync(KEY_FILE, 'utf8'));
  privPem = stored.privPem;
  pubPem = stored.pubPem;
  console.log('[ok] reusing existing demo keypair from demo-key.json');
} else {
  const { privateKey, publicKey } = generateKeyPairSync('ed25519');
  privPem = privateKey.export({ format: 'pem', type: 'pkcs8' }).toString();
  pubPem = publicKey.export({ format: 'pem', type: 'spki' }).toString();
  writeFileSync(KEY_FILE, JSON.stringify({
    note: 'Demo keypair for the showcase. NEVER use in production. Per-deployment keys per ADR-0011.',
    keyid: 'demo-key-2026',
    privPem,
    pubPem,
  }, null, 2) + '\n');
  console.log('[ok] generated new demo keypair → demo-key.json');
}

// Canonicalize the manifest (stable JSON), hash it, sign the hash.
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const canonical = canonicalize(manifest);
const messageHashHex = createHash('sha256').update(canonical).digest('hex');
const messageBuf = Buffer.from(canonical, 'utf8');

const signature = sign(null, messageBuf, {
  key: privPem,
  format: 'pem',
  type: 'pkcs8',
});

const dsse = JSON.parse(readFileSync(dssePath, 'utf8'));
dsse.signature = {
  keyid: 'demo-key-2026',
  alg: 'ed25519',
  sig_b64: signature.toString('base64'),
  message_hash_sha256: messageHashHex,
  canonical_message_length: messageBuf.length,
};
dsse._note = 'This signature is real ed25519. The browser verifies it via SubtleCrypto.';
writeFileSync(dssePath, JSON.stringify(dsse, null, 2) + '\n');

// Public key as PEM (also written as JSON for easy import)
writeFileSync(join(BUNDLE, 'public-key.pem'), pubPem);
writeFileSync(join(BUNDLE, 'public-key.json'), JSON.stringify({
  alg: 'ed25519',
  keyid: 'demo-key-2026',
  pubPem,
  note: 'Demo key for the showcase site only. Real evidence bundles use a per-deployment ed25519 key (see ADR-0011).',
}, null, 2) + '\n');

console.log(`[ok] signed manifest`);
console.log(`  keyid:      demo-key-2026`);
console.log(`  hash(SHA256): ${messageHashHex.slice(0, 32)}...`);
console.log(`  sig(b64):     ${signature.toString('base64').slice(0, 32)}...`);

function canonicalize(obj) {
  // RFC 8785 / JCS-like — stable JSON: sort object keys recursively, no whitespace.
  if (obj === null || typeof obj !== 'object') return JSON.stringify(obj);
  if (Array.isArray(obj)) return '[' + obj.map(canonicalize).join(',') + ']';
  const keys = Object.keys(obj).sort();
  return '{' + keys.map((k) => JSON.stringify(k) + ':' + canonicalize(obj[k])).join(',') + '}';
}
