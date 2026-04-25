// Deft Vault — client-side cryptography
// =====================================================================
// All key material is derived and used in the user's browser; the
// server only ever sees ciphertext. We use:
//   • Argon2id (m=64MiB, t=3, p=4) via hash-wasm → 32-byte KEK
//   • AES-256-GCM via WebCrypto with a fresh 12-byte IV per encryption
//   • RFC 4648 base32 (no padding, hyphenated in groups of 4) for the
//     24-character recovery code shown once at vault setup.
//
// Every blob written to the database is `IV || ciphertext || GCM-tag`
// emitted by WebCrypto's encrypt() (which appends the 16-byte tag to
// the ciphertext automatically).
// =====================================================================

import { argon2id } from "hash-wasm";

// ---------------------------------------------------------------------
// KDF parameters (mirror the DB CHECK constraints exactly)
// ---------------------------------------------------------------------
export interface KdfParams {
  algorithm: "argon2id";
  memoryKiB: number;
  iterations: number;
  parallelism: number;
}

export const DEFAULT_KDF: KdfParams = Object.freeze({
  algorithm: "argon2id",
  memoryKiB: 65536, // 64 MiB
  iterations: 3,
  parallelism: 4,
});

// ---------------------------------------------------------------------
// Byte / base64 helpers
// ---------------------------------------------------------------------
export function randomBytes(n: number): Uint8Array {
  const out = new Uint8Array(n);
  crypto.getRandomValues(out);
  return out;
}

export function bytesToB64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}

export function b64ToBytes(b64: string): Uint8Array {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

const ENC = new TextEncoder();
const DEC = new TextDecoder();

// ---------------------------------------------------------------------
// Argon2id key derivation
// ---------------------------------------------------------------------
async function deriveKEK(
  password: string,
  salt: Uint8Array,
  kdf: KdfParams = DEFAULT_KDF,
): Promise<Uint8Array> {
  if (kdf.algorithm !== "argon2id") {
    throw new Error(`unsupported KDF algorithm: ${kdf.algorithm}`);
  }
  if (salt.length !== 16) throw new Error("salt must be 16 bytes");
  // hash-wasm returns hex when outputType is "hex" (the default for
  // argon2id). Use Uint8Array output for direct key material.
  const out = await argon2id({
    password,
    salt,
    parallelism: kdf.parallelism,
    iterations: kdf.iterations,
    memorySize: kdf.memoryKiB,
    hashLength: 32,
    outputType: "binary",
  });
  if (!(out instanceof Uint8Array) || out.length !== 32) {
    throw new Error("argon2id returned unexpected output");
  }
  return out;
}

async function importAesGcmKey(rawKey: Uint8Array): Promise<CryptoKey> {
  if (rawKey.length !== 32) throw new Error("AES key must be 32 bytes");
  return crypto.subtle.importKey(
    "raw",
    rawKey,
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
}

// ---------------------------------------------------------------------
// AES-256-GCM encrypt / decrypt primitives
// ---------------------------------------------------------------------
export async function encryptGcm(
  rawKey: Uint8Array,
  plaintext: Uint8Array,
): Promise<{ iv: Uint8Array; blob: Uint8Array }> {
  const key = await importAesGcmKey(rawKey);
  const iv = randomBytes(12);
  const cipherBuf = await crypto.subtle.encrypt(
    { name: "AES-GCM", iv },
    key,
    plaintext,
  );
  return { iv, blob: new Uint8Array(cipherBuf) };
}

export async function decryptGcm(
  rawKey: Uint8Array,
  iv: Uint8Array,
  blob: Uint8Array,
): Promise<Uint8Array> {
  if (iv.length !== 12) throw new Error("iv must be 12 bytes");
  const key = await importAesGcmKey(rawKey);
  const buf = await crypto.subtle.decrypt({ name: "AES-GCM", iv }, key, blob);
  return new Uint8Array(buf);
}

// ---------------------------------------------------------------------
// Recovery code: 24-char base32 (Crockford alphabet, hyphenated)
// ---------------------------------------------------------------------
const B32_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"; // 30 chars; close to 32-alphabet for human reads

export function generateRecoveryCode(): string {
  // 24 chars × log2(30) ≈ 117 bits of entropy, well above the 80-bit
  // floor we want for a long-lived recovery secret.
  const rand = new Uint32Array(24);
  crypto.getRandomValues(rand);
  let out = "";
  for (let i = 0; i < 24; i++) {
    out += B32_ALPHABET[rand[i] % B32_ALPHABET.length];
    if (i % 4 === 3 && i !== 23) out += "-";
  }
  return out;
}

export function normalizeRecoveryCode(code: string): string {
  return code.replace(/[\s-]/g, "").toUpperCase();
}

// ---------------------------------------------------------------------
// Vault setup — produces all blobs the server needs to persist
// ---------------------------------------------------------------------
export interface VaultSetupResult {
  recoveryCode: string;
  masterKey: Uint8Array;
  // Server-bound payload (raw bytes; UI converts to base64 before POST)
  passphraseSalt: Uint8Array;
  passphraseIv: Uint8Array;
  passphraseBlob: Uint8Array;
  recoverySalt: Uint8Array;
  recoveryIv: Uint8Array;
  recoveryBlob: Uint8Array;
  verifierIv: Uint8Array;
  verifierBlob: Uint8Array;
}

export async function setupVault(passphrase: string): Promise<VaultSetupResult> {
  if (typeof passphrase !== "string" || passphrase.length < 12) {
    throw new Error("passphrase must be at least 12 characters");
  }

  // 1. Generate the master key.
  const masterKey = randomBytes(32);

  // 2. Wrap under passphrase-derived KEK.
  const passphraseSalt = randomBytes(16);
  const kekP = await deriveKEK(passphrase, passphraseSalt);
  const wrappedP = await encryptGcm(kekP, masterKey);

  // 3. Wrap under recovery-code-derived KEK.
  const recoveryCode = generateRecoveryCode();
  const recoverySalt = randomBytes(16);
  const kekR = await deriveKEK(normalizeRecoveryCode(recoveryCode), recoverySalt);
  const wrappedR = await encryptGcm(kekR, masterKey);

  // 4. Verifier — 32 bytes of public-known plaintext we can re-decrypt
  //    after unlock to confirm the masterKey is correct.
  const verifierPlain = ENC.encode("DEFT_VAULT_OK_v1");
  const verifierWrapped = await encryptGcm(masterKey, verifierPlain);

  return {
    recoveryCode,
    masterKey,
    passphraseSalt,
    passphraseIv: wrappedP.iv,
    passphraseBlob: wrappedP.blob,
    recoverySalt,
    recoveryIv: wrappedR.iv,
    recoveryBlob: wrappedR.blob,
    verifierIv: verifierWrapped.iv,
    verifierBlob: verifierWrapped.blob,
  };
}

// ---------------------------------------------------------------------
// Vault unlock — passphrase OR recovery code
// ---------------------------------------------------------------------
export interface ServerVaultMeta {
  kdfMemoryKiB: number;
  kdfIterations: number;
  kdfParallelism: number;
  passphraseSalt: Uint8Array;
  passphraseIv: Uint8Array;
  passphraseBlob: Uint8Array;
  recoverySalt: Uint8Array;
  recoveryIv: Uint8Array;
  recoveryBlob: Uint8Array;
  verifierIv: Uint8Array;
  verifierBlob: Uint8Array;
}

const VERIFIER_EXPECTED = "DEFT_VAULT_OK_v1";

async function tryUnwrap(
  kek: Uint8Array,
  iv: Uint8Array,
  blob: Uint8Array,
  meta: ServerVaultMeta,
): Promise<Uint8Array> {
  const candidate = await decryptGcm(kek, iv, blob);
  // Verifier check — confirm the unwrapped key actually decrypts the
  // verifier blob the server stored at setup time.
  const verifierPlain = await decryptGcm(candidate, meta.verifierIv, meta.verifierBlob);
  if (DEC.decode(verifierPlain) !== VERIFIER_EXPECTED) {
    throw new Error("verifier mismatch — wrong key");
  }
  return candidate;
}

export async function unlockWithPassphrase(
  passphrase: string,
  meta: ServerVaultMeta,
): Promise<Uint8Array> {
  const kdf: KdfParams = {
    algorithm: "argon2id",
    memoryKiB: meta.kdfMemoryKiB,
    iterations: meta.kdfIterations,
    parallelism: meta.kdfParallelism,
  };
  const kek = await deriveKEK(passphrase, meta.passphraseSalt, kdf);
  return tryUnwrap(kek, meta.passphraseIv, meta.passphraseBlob, meta);
}

export async function unlockWithRecoveryCode(
  code: string,
  meta: ServerVaultMeta,
): Promise<Uint8Array> {
  const normalized = normalizeRecoveryCode(code);
  if (normalized.length !== 24) {
    throw new Error("recovery code must be 24 characters (excluding hyphens)");
  }
  const kdf: KdfParams = {
    algorithm: "argon2id",
    memoryKiB: meta.kdfMemoryKiB,
    iterations: meta.kdfIterations,
    parallelism: meta.kdfParallelism,
  };
  const kek = await deriveKEK(normalized, meta.recoverySalt, kdf);
  return tryUnwrap(kek, meta.recoveryIv, meta.recoveryBlob, meta);
}

// ---------------------------------------------------------------------
// Per-secret encrypt / decrypt (under the unlocked masterKey)
// ---------------------------------------------------------------------
export interface SecretCiphertext {
  valueIv: Uint8Array;
  valueBlob: Uint8Array;
  previewIv: Uint8Array;
  previewBlob: Uint8Array;
}

export async function encryptSecret(
  masterKey: Uint8Array,
  plaintext: string,
): Promise<SecretCiphertext> {
  if (typeof plaintext !== "string" || plaintext.length === 0) {
    throw new Error("secret plaintext must be a non-empty string");
  }
  if (plaintext.length > 16384) {
    throw new Error("secret too long (max 16384 chars)");
  }
  const valueWrap = await encryptGcm(masterKey, ENC.encode(plaintext));
  // Preview = last 4 plaintext characters, separately encrypted so the
  // UI can show "····abcd" without ever transmitting more.
  const tail = plaintext.length >= 4 ? plaintext.slice(-4) : plaintext;
  const previewWrap = await encryptGcm(masterKey, ENC.encode(tail));
  return {
    valueIv: valueWrap.iv,
    valueBlob: valueWrap.blob,
    previewIv: previewWrap.iv,
    previewBlob: previewWrap.blob,
  };
}

export async function decryptSecret(
  masterKey: Uint8Array,
  ct: SecretCiphertext,
): Promise<string> {
  const buf = await decryptGcm(masterKey, ct.valueIv, ct.valueBlob);
  return DEC.decode(buf);
}

export async function decryptPreview(
  masterKey: Uint8Array,
  ct: { previewIv: Uint8Array; previewBlob: Uint8Array },
): Promise<string> {
  const buf = await decryptGcm(masterKey, ct.previewIv, ct.previewBlob);
  return DEC.decode(buf);
}

// ---------------------------------------------------------------------
// Self-test (dev only — exposed for the dev console / smoke runs)
// ---------------------------------------------------------------------
export async function selfTest(): Promise<{ ok: true } | { ok: false; error: string }> {
  try {
    const setup = await setupVault("a-strong-passphrase!");
    const meta: ServerVaultMeta = {
      kdfMemoryKiB: DEFAULT_KDF.memoryKiB,
      kdfIterations: DEFAULT_KDF.iterations,
      kdfParallelism: DEFAULT_KDF.parallelism,
      passphraseSalt: setup.passphraseSalt,
      passphraseIv: setup.passphraseIv,
      passphraseBlob: setup.passphraseBlob,
      recoverySalt: setup.recoverySalt,
      recoveryIv: setup.recoveryIv,
      recoveryBlob: setup.recoveryBlob,
      verifierIv: setup.verifierIv,
      verifierBlob: setup.verifierBlob,
    };
    const k1 = await unlockWithPassphrase("a-strong-passphrase!", meta);
    if (bytesToB64(k1) !== bytesToB64(setup.masterKey)) {
      return { ok: false, error: "passphrase unlock did not recover masterKey" };
    }
    const k2 = await unlockWithRecoveryCode(setup.recoveryCode, meta);
    if (bytesToB64(k2) !== bytesToB64(setup.masterKey)) {
      return { ok: false, error: "recovery unlock did not recover masterKey" };
    }
    const ct = await encryptSecret(setup.masterKey, "sk-this-is-a-test-key-12345");
    const pt = await decryptSecret(setup.masterKey, ct);
    if (pt !== "sk-this-is-a-test-key-12345") {
      return { ok: false, error: "decryptSecret round-trip failed" };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}
