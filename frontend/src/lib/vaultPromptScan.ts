/**
 * Vault prompt scanning + resolution.
 *
 * The agent prompt language supports a `$KEY_NAME` sentinel that the
 * frontend resolves at submit time by decrypting the matching vault
 * secret locally and forwarding the plaintext to the orchestrator under
 * `vault_env` (Redis-backed, per-task, auto-purged on terminal state).
 *
 * Grammar (mirrors the server-side store):
 *   $[A-Z][A-Z0-9_]{0,63}
 *
 * The leading `$` must NOT be preceded by another `$` (so escaping with
 * `$$` is supported and yields no match) or by an identifier character
 * (so `foo$BAR` does not match — vault refs always stand alone).
 */

// Reference detection regex.  The lookbehind ensures we don't match
// `$$ESCAPED` (a doubled-dollar literal) or identifier-glued tokens.
const REF_RE = /(?<![A-Za-z0-9_$])\$([A-Z][A-Z0-9_]{0,63})\b/g;

export interface VaultRefScan {
  /** Distinct uppercase secret names referenced in the prompt. */
  names: string[];
  /** Total number of occurrences across the prompt. */
  occurrences: number;
}

export function scanVaultRefs(prompt: string): VaultRefScan {
  const names = new Set<string>();
  let occurrences = 0;
  if (!prompt) return { names: [], occurrences: 0 };
  for (const m of prompt.matchAll(REF_RE)) {
    const name = m[1];
    if (!name) continue;
    names.add(name);
    occurrences += 1;
  }
  return { names: [...names].sort(), occurrences };
}

/**
 * Resolve every name in `names` to plaintext via `decrypt`.  Throws on
 * the first missing/failed secret with a message that names the offending
 * key — surfaced to the user so they know exactly what to add to the vault.
 */
export async function resolveVaultRefs(
  names: string[],
  decrypt: (name: string) => Promise<string>,
): Promise<Record<string, string>> {
  const out: Record<string, string> = {};
  for (const n of names) {
    try {
      out[n] = await decrypt(n);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // Re-raise with a stable shape so the UI can show "Add $X to your vault".
      throw new VaultRefError(n, msg);
    }
  }
  return out;
}

export class VaultRefError extends Error {
  readonly missingName: string;
  constructor(name: string, cause: string) {
    super(`Vault secret $${name} is not available: ${cause}`);
    this.name = "VaultRefError";
    this.missingName = name;
  }
}
