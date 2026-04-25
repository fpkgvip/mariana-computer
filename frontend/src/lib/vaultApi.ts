// Deft vault REST client — speaks base64-encoded ciphertext only.
// All crypto lives in vaultCrypto.ts; this file is pure plumbing.

import { api, ApiError } from "@/lib/api";
import {
  bytesToB64,
  b64ToBytes,
  encryptSecret,
  setupVault,
  unlockWithPassphrase,
  unlockWithRecoveryCode,
  type ServerVaultMeta,
  type VaultSetupResult,
} from "@/lib/vaultCrypto";

// ---------------------------------------------------------------------
// Wire types (mirror mariana/vault/router.py)
// ---------------------------------------------------------------------
export interface VaultDTO {
  user_id: string;
  kdf_algorithm: "argon2id";
  kdf_memory_kib: number;
  kdf_iterations: number;
  kdf_parallelism: number;
  passphrase_salt: string;
  passphrase_iv: string;
  passphrase_blob: string;
  recovery_salt: string;
  recovery_iv: string;
  recovery_blob: string;
  verifier_iv: string;
  verifier_blob: string;
  created_at: string;
  updated_at: string;
}

export interface SecretDTO {
  id: string;
  name: string;
  description: string | null;
  value_iv: string;
  value_blob: string;
  preview_iv: string;
  preview_blob: string;
  created_at: string;
  updated_at: string;
}

// ---------------------------------------------------------------------
// Vault root
// ---------------------------------------------------------------------
export async function fetchVault(): Promise<VaultDTO | null> {
  try {
    return await api.get<VaultDTO>("/api/vault");
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) return null;
    throw e;
  }
}

export function vaultDtoToMeta(dto: VaultDTO): ServerVaultMeta {
  return {
    kdfMemoryKiB: dto.kdf_memory_kib,
    kdfIterations: dto.kdf_iterations,
    kdfParallelism: dto.kdf_parallelism,
    passphraseSalt: b64ToBytes(dto.passphrase_salt),
    passphraseIv: b64ToBytes(dto.passphrase_iv),
    passphraseBlob: b64ToBytes(dto.passphrase_blob),
    recoverySalt: b64ToBytes(dto.recovery_salt),
    recoveryIv: b64ToBytes(dto.recovery_iv),
    recoveryBlob: b64ToBytes(dto.recovery_blob),
    verifierIv: b64ToBytes(dto.verifier_iv),
    verifierBlob: b64ToBytes(dto.verifier_blob),
  };
}

export async function createVaultOnServer(setup: VaultSetupResult): Promise<VaultDTO> {
  return await api.post<VaultDTO>("/api/vault", {
    passphrase_salt: bytesToB64(setup.passphraseSalt),
    passphrase_iv: bytesToB64(setup.passphraseIv),
    passphrase_blob: bytesToB64(setup.passphraseBlob),
    recovery_salt: bytesToB64(setup.recoverySalt),
    recovery_iv: bytesToB64(setup.recoveryIv),
    recovery_blob: bytesToB64(setup.recoveryBlob),
    verifier_iv: bytesToB64(setup.verifierIv),
    verifier_blob: bytesToB64(setup.verifierBlob),
  });
}

export async function deleteVaultOnServer(): Promise<void> {
  await api.delete("/api/vault");
}

// ---------------------------------------------------------------------
// Secrets list / mutate
// ---------------------------------------------------------------------
export async function listSecrets(): Promise<SecretDTO[]> {
  return await api.get<SecretDTO[]>("/api/vault/secrets");
}

export async function createSecretOnServer(
  masterKey: Uint8Array,
  name: string,
  plaintext: string,
  description?: string,
): Promise<SecretDTO> {
  const ct = await encryptSecret(masterKey, plaintext);
  return await api.post<SecretDTO>("/api/vault/secrets", {
    name,
    description: description ?? null,
    value_iv: bytesToB64(ct.valueIv),
    value_blob: bytesToB64(ct.valueBlob),
    preview_iv: bytesToB64(ct.previewIv),
    preview_blob: bytesToB64(ct.previewBlob),
  });
}

export async function updateSecretOnServer(
  masterKey: Uint8Array,
  id: string,
  plaintext: string,
  description?: string,
): Promise<SecretDTO> {
  const ct = await encryptSecret(masterKey, plaintext);
  return await api.patch<SecretDTO>(`/api/vault/secrets/${id}`, {
    value_iv: bytesToB64(ct.valueIv),
    value_blob: bytesToB64(ct.valueBlob),
    preview_iv: bytesToB64(ct.previewIv),
    preview_blob: bytesToB64(ct.previewBlob),
    description: description ?? null,
  });
}

export async function deleteSecretOnServer(id: string): Promise<void> {
  await api.delete(`/api/vault/secrets/${id}`);
}

// ---------------------------------------------------------------------
// High-level convenience flows
// ---------------------------------------------------------------------
export async function setupAndPublishVault(passphrase: string): Promise<{
  recoveryCode: string;
  masterKey: Uint8Array;
  vault: VaultDTO;
}> {
  const setup = await setupVault(passphrase);
  const vault = await createVaultOnServer(setup);
  return { recoveryCode: setup.recoveryCode, masterKey: setup.masterKey, vault };
}

export async function unlockVault(
  vault: VaultDTO,
  options: { passphrase?: string; recoveryCode?: string },
): Promise<Uint8Array> {
  const meta = vaultDtoToMeta(vault);
  if (options.passphrase) {
    return unlockWithPassphrase(options.passphrase, meta);
  }
  if (options.recoveryCode) {
    return unlockWithRecoveryCode(options.recoveryCode, meta);
  }
  throw new Error("must provide passphrase or recoveryCode");
}
