/**
 * useVault — single source of truth for the user's Deft Vault state.
 *
 * Responsibilities:
 *  • Fetch the server-side vault metadata once per auth session.
 *  • Hold the unlocked masterKey IN MEMORY ONLY (never localStorage/sessionStorage).
 *  • Auto-lock after 30 minutes of inactivity (configurable via VITE_VAULT_LOCK_MS).
 *  • Broadcast a custom event ("deft:vault-changed") so other components refresh.
 *  • Expose imperative helpers: setup, unlockPassphrase, unlockRecovery, lock,
 *    addSecret, updateSecret, deleteSecret, refreshSecrets, decryptByName.
 *
 * Security notes:
 *  - The masterKey is held in a React state in this module-level singleton store
 *    so all consumers share the exact same Uint8Array. We do NOT serialize it.
 *  - On lock(), we zero the bytes so any GC leak still carries no entropy.
 *  - On window "beforeunload" we lock proactively.
 *  - We never log or stringify the masterKey or any plaintext secret.
 */
import { useCallback, useEffect, useState } from "react";
import { useAuth } from "@/contexts/AuthContext";
import {
  fetchVault,
  vaultDtoToMeta,
  setupAndPublishVault,
  unlockVault,
  listSecrets,
  createSecretOnServer,
  updateSecretOnServer,
  deleteSecretOnServer,
  deleteVaultOnServer,
  type VaultDTO,
  type SecretDTO,
} from "@/lib/vaultApi";
import { decryptSecret, decryptPreview, b64ToBytes as bytesFromB64 } from "@/lib/vaultCrypto";
import { ApiError } from "@/lib/api";

export const VAULT_CHANGED_EVENT = "deft:vault-changed";
const DEFAULT_LOCK_MS = 30 * 60 * 1000; // 30 min
const LOCK_MS = (() => {
  const raw = (import.meta.env as { VITE_VAULT_LOCK_MS?: string }).VITE_VAULT_LOCK_MS;
  const n = raw ? Number(raw) : NaN;
  return Number.isFinite(n) && n > 0 ? n : DEFAULT_LOCK_MS;
})();

// ---------------------------------------------------------------------
// Module-level singleton state — every useVault() call shares this.
// ---------------------------------------------------------------------
type Listener = () => void;

interface VaultStore {
  vault: VaultDTO | null;            // server metadata (or null = not yet set up)
  loaded: boolean;                   // initial fetch complete
  loadError: string | null;
  masterKey: Uint8Array | null;      // present iff unlocked
  secrets: SecretDTO[];
  lastActivity: number;
}

const store: VaultStore = {
  vault: null,
  loaded: false,
  loadError: null,
  masterKey: null,
  secrets: [],
  lastActivity: Date.now(),
};

const listeners = new Set<Listener>();
function emit() {
  for (const l of listeners) l();
  try {
    window.dispatchEvent(new Event(VAULT_CHANGED_EVENT));
  } catch {
    /* SSR-safe noop */
  }
}

function zero(buf: Uint8Array | null) {
  if (!buf) return;
  try {
    buf.fill(0);
  } catch {
    /* noop */
  }
}

function bumpActivity() {
  store.lastActivity = Date.now();
}

let activeUserId: string | null = null;
let loadInFlight: Promise<void> | null = null;

// ---------------------------------------------------------------------
// Auto-lock timer (single global)
// ---------------------------------------------------------------------
let lockInterval: number | null = null;
function ensureLockTimer() {
  if (lockInterval !== null) return;
  lockInterval = window.setInterval(() => {
    if (!store.masterKey) return;
    if (Date.now() - store.lastActivity > LOCK_MS) {
      lockNow();
    }
  }, 30_000);
}
function clearLockTimer() {
  if (lockInterval !== null) {
    window.clearInterval(lockInterval);
    lockInterval = null;
  }
}

function lockNow() {
  zero(store.masterKey);
  store.masterKey = null;
  store.secrets = []; // forget previews too
  emit();
}

if (typeof window !== "undefined") {
  window.addEventListener("beforeunload", () => lockNow());
  // Track activity (any click/keydown counts).
  const mark = () => bumpActivity();
  window.addEventListener("click", mark, { passive: true });
  window.addEventListener("keydown", mark, { passive: true });
}

// ---------------------------------------------------------------------
// Loaders
// ---------------------------------------------------------------------
async function loadVault(): Promise<void> {
  if (loadInFlight) return loadInFlight;
  const userAtStart = activeUserId;
  store.loaded = false;
  store.loadError = null;
  emit();
  loadInFlight = (async () => {
    try {
      const v = await fetchVault();
      if (activeUserId !== userAtStart) return; // user changed mid-flight
      store.vault = v;
      store.loaded = true;
      store.loadError = null;
    } catch (e) {
      if (activeUserId !== userAtStart) return;
      store.loaded = true;
      store.loadError = e instanceof ApiError ? e.message : String(e);
    } finally {
      emit();
    }
  })().finally(() => {
    loadInFlight = null;
  });
  return loadInFlight;
}

async function loadSecrets() {
  if (!store.masterKey) return;
  try {
    store.secrets = await listSecrets();
  } catch (e) {
    store.loadError = e instanceof ApiError ? e.message : String(e);
  }
  emit();
}

// ---------------------------------------------------------------------
// Public mutations
// ---------------------------------------------------------------------
async function setupVaultAndUnlock(passphrase: string): Promise<{ recoveryCode: string }> {
  const { recoveryCode, masterKey, vault } = await setupAndPublishVault(passphrase);
  store.vault = vault;
  store.masterKey = masterKey;
  store.secrets = [];
  bumpActivity();
  ensureLockTimer();
  emit();
  return { recoveryCode };
}

async function unlockWithPassphrase(passphrase: string) {
  if (!store.vault) throw new Error("vault is not set up yet");
  const k = await unlockVault(store.vault, { passphrase });
  store.masterKey = k;
  bumpActivity();
  ensureLockTimer();
  await loadSecrets();
}

async function unlockWithRecoveryCode(code: string) {
  if (!store.vault) throw new Error("vault is not set up yet");
  const k = await unlockVault(store.vault, { recoveryCode: code });
  store.masterKey = k;
  bumpActivity();
  ensureLockTimer();
  await loadSecrets();
}

async function addSecret(name: string, plaintext: string, description?: string) {
  if (!store.masterKey) throw new Error("vault is locked");
  await createSecretOnServer(store.masterKey, name, plaintext, description);
  bumpActivity();
  await loadSecrets();
}

async function updateSecret(id: string, plaintext: string, description?: string) {
  if (!store.masterKey) throw new Error("vault is locked");
  await updateSecretOnServer(store.masterKey, id, plaintext, description);
  bumpActivity();
  await loadSecrets();
}

async function deleteSecret(id: string) {
  await deleteSecretOnServer(id);
  bumpActivity();
  await loadSecrets();
}

async function destroyVault() {
  await deleteVaultOnServer();
  zero(store.masterKey);
  store.masterKey = null;
  store.vault = null;
  store.secrets = [];
  emit();
}

async function decryptByName(name: string): Promise<string> {
  if (!store.masterKey) throw new Error("vault is locked");
  const s = store.secrets.find((x) => x.name === name);
  if (!s) throw new Error(`secret not found: ${name}`);
  return decryptSecret(store.masterKey, {
    valueIv: bytesFromB64(s.value_iv),
    valueBlob: bytesFromB64(s.value_blob),
    previewIv: bytesFromB64(s.preview_iv),
    previewBlob: bytesFromB64(s.preview_blob),
  });
}

// ---------------------------------------------------------------------
// React hook
// ---------------------------------------------------------------------
export interface UseVaultState {
  loaded: boolean;
  loadError: string | null;
  exists: boolean;          // server has a vault row
  unlocked: boolean;        // we hold the masterKey in memory
  vault: VaultDTO | null;
  secrets: SecretDTO[];
  setup: (passphrase: string) => Promise<{ recoveryCode: string }>;
  unlockPassphrase: (passphrase: string) => Promise<void>;
  unlockRecovery: (code: string) => Promise<void>;
  lock: () => void;
  reload: () => Promise<void>;
  addSecret: (name: string, plaintext: string, description?: string) => Promise<void>;
  updateSecret: (id: string, plaintext: string, description?: string) => Promise<void>;
  deleteSecret: (id: string) => Promise<void>;
  destroyVault: () => Promise<void>;
  decryptByName: (name: string) => Promise<string>;
  decryptPreviewFor: (s: SecretDTO) => Promise<string>;
}

export function useVault(): UseVaultState {
  const { user } = useAuth();
  const userId = user?.id ?? null;
  const [, setTick] = useState(0);

  // Subscribe to store changes.
  useEffect(() => {
    const l = () => setTick((n) => n + 1);
    listeners.add(l);
    return () => {
      listeners.delete(l);
    };
  }, []);

  // Refetch on user change (compared by stable id, not object reference).
  // Lock if we lose the user.
  useEffect(() => {
    if (userId === activeUserId && (store.loaded || loadInFlight)) {
      // Already loaded for this user — no work.
      return;
    }
    activeUserId = userId;
    if (!userId) {
      lockNow();
      store.vault = null;
      store.loaded = false;
      store.loadError = null;
      clearLockTimer();
      emit();
      return;
    }
    void loadVault();
  }, [userId]);

  const decryptPreviewFor = useCallback(async (s: SecretDTO): Promise<string> => {
    if (!store.masterKey) throw new Error("vault is locked");
    return decryptPreview(store.masterKey, {
      previewIv: bytesFromB64(s.preview_iv),
      previewBlob: bytesFromB64(s.preview_blob),
    });
  }, []);

  return {
    loaded: store.loaded,
    loadError: store.loadError,
    exists: store.vault !== null,
    unlocked: store.masterKey !== null,
    vault: store.vault,
    secrets: store.secrets,
    setup: setupVaultAndUnlock,
    unlockPassphrase: unlockWithPassphrase,
    unlockRecovery: unlockWithRecoveryCode,
    lock: lockNow,
    reload: loadVault,
    addSecret,
    updateSecret,
    deleteSecret,
    destroyVault,
    decryptByName,
    decryptPreviewFor,
  };
}
