/**
 * SecretsTable — production wrapper that wires useVault to SecretsTableView.
 *
 * Decryption happens entirely in the browser. We deliberately render previews
 * lazily (one decrypt per row, on mount) so the UI never requests plaintext
 * we don't show.
 */
import { useEffect, useState } from "react";
import { useVault } from "@/hooks/useVault";
import { toast } from "sonner";
import type { SecretDTO } from "@/lib/vaultApi";
import { AddSecretDialog } from "./AddSecretDialog";
import { SecretsTableView } from "./vault/SecretsTableView";
import { SecretsEmptyState } from "./vault/SecretsEmptyState";

export function SecretsTable() {
  const vault = useVault();
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<SecretDTO | null>(null);
  const [adding, setAdding] = useState(false);

  // Decrypt previews whenever the secret list changes (and we're unlocked).
  // Depend only on stable scalars (a fingerprint of secret IDs) to avoid
  // re-running on every store emit.
  const fingerprint = vault.secrets.map((s) => s.id + s.updated_at).join("|");
  const unlocked = vault.unlocked;
  useEffect(() => {
    if (!unlocked) return;
    let cancelled = false;
    (async () => {
      const next: Record<string, string> = {};
      for (const s of vault.secrets) {
        try {
          next[s.id] = await vault.decryptPreviewFor(s);
        } catch {
          next[s.id] = "····";
        }
      }
      if (!cancelled) setPreviews(next);
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fingerprint, unlocked]);

  const copyValue = async (s: SecretDTO) => {
    try {
      const plaintext = await vault.decryptByName(s.name);
      await navigator.clipboard.writeText(plaintext);
      toast.success(`${s.name} copied to clipboard.`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not copy to clipboard.");
    }
  };

  const copySentinel = async (s: SecretDTO) => {
    try {
      await navigator.clipboard.writeText(`$${s.name}`);
      toast.success(`$${s.name} copied. Paste into a prompt.`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not copy to clipboard.");
    }
  };

  const handleDelete = async (s: SecretDTO) => {
    try {
      await vault.deleteSecret(s.id);
      toast.success(`Deleted ${s.name}.`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Delete failed.");
    }
  };

  if (vault.secrets.length === 0) {
    return (
      <>
        <SecretsEmptyState onAdd={() => setAdding(true)} />
        <AddSecretDialog open={adding} onClose={() => setAdding(false)} />
      </>
    );
  }

  return (
    <>
      <SecretsTableView
        secrets={vault.secrets}
        previews={previews}
        onCopyValue={copyValue}
        onCopySentinel={copySentinel}
        onEdit={(s) => setEditing(s)}
        onDelete={handleDelete}
        onAdd={() => setAdding(true)}
      />
      <AddSecretDialog open={adding} onClose={() => setAdding(false)} />
      <AddSecretDialog
        open={editing !== null}
        onClose={() => setEditing(null)}
        editing={editing ?? undefined}
      />
    </>
  );
}
