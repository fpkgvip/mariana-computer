/**
 * SecretsTable — list of vault secrets with masked previews + actions.
 *
 * Decryption happens entirely in the browser. We deliberately render previews
 * lazily (one decrypt per row, on mount) so the UI never requests plaintext
 * we don't show.
 */
import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Pencil, Trash2, Copy, Check, Plus, KeyRound } from "lucide-react";
import { useVault } from "@/hooks/useVault";
import { toast } from "sonner";
import type { SecretDTO } from "@/lib/vaultApi";
import { AddSecretDialog } from "./AddSecretDialog";

export function SecretsTable() {
  const vault = useVault();
  const [previews, setPreviews] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState<SecretDTO | null>(null);
  const [adding, setAdding] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<SecretDTO | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

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
      setCopiedId(s.id);
      toast.success(`${s.name} copied to clipboard.`);
      window.setTimeout(() => setCopiedId(null), 2000);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not copy.");
    }
  };

  const handleDelete = async () => {
    if (!confirmDelete) return;
    try {
      await vault.deleteSecret(confirmDelete.id);
      toast.success(`Deleted ${confirmDelete.name}.`);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Delete failed.");
    } finally {
      setConfirmDelete(null);
    }
  };

  if (vault.secrets.length === 0) {
    return (
      <>
        <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-border px-6 py-12 text-center">
          <div className="rounded-full bg-primary/10 p-3">
            <KeyRound size={20} className="text-primary" />
          </div>
          <div>
            <p className="text-sm font-medium text-foreground">No secrets yet</p>
            <p className="mx-auto mt-1 max-w-sm text-xs text-muted-foreground">
              Add API keys here, then reference them in your prompts as <code className="rounded bg-muted px-1">$KEY_NAME</code>.
            </p>
          </div>
          <Button onClick={() => setAdding(true)} size="sm">
            <Plus size={14} className="mr-1.5" />
            Add your first secret
          </Button>
        </div>
        <AddSecretDialog open={adding} onClose={() => setAdding(false)} />
      </>
    );
  }

  return (
    <>
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          {vault.secrets.length} secret{vault.secrets.length === 1 ? "" : "s"}
        </p>
        <Button onClick={() => setAdding(true)} size="sm">
          <Plus size={14} className="mr-1.5" />
          Add secret
        </Button>
      </div>

      <div className="overflow-hidden rounded-lg border border-border">
        <table className="w-full text-sm">
          <thead className="bg-muted/40 text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="px-4 py-2 text-left font-medium">Name</th>
              <th className="px-4 py-2 text-left font-medium">Value</th>
              <th className="px-4 py-2 text-left font-medium">Description</th>
              <th className="px-4 py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {vault.secrets.map((s) => (
              <tr key={s.id} className="border-t border-border">
                <td className="px-4 py-2 font-mono text-[13px] text-foreground">{s.name}</td>
                <td className="px-4 py-2 font-mono text-xs text-muted-foreground">
                  ····{previews[s.id] ?? "····"}
                </td>
                <td className="px-4 py-2 text-muted-foreground">{s.description ?? "—"}</td>
                <td className="px-4 py-2">
                  <div className="flex justify-end gap-1">
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => copyValue(s)}
                      aria-label={`Copy ${s.name}`}
                      title="Copy to clipboard"
                    >
                      {copiedId === s.id ? <Check size={14} /> : <Copy size={14} />}
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => setEditing(s)}
                      aria-label={`Edit ${s.name}`}
                      title="Edit value"
                    >
                      <Pencil size={14} />
                    </Button>
                    <Button
                      size="icon"
                      variant="ghost"
                      onClick={() => setConfirmDelete(s)}
                      aria-label={`Delete ${s.name}`}
                      title="Delete"
                      className="text-red-400 hover:text-red-300"
                    >
                      <Trash2 size={14} />
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <AddSecretDialog open={adding} onClose={() => setAdding(false)} />
      <AddSecretDialog
        open={editing !== null}
        onClose={() => setEditing(null)}
        editing={editing ?? undefined}
      />

      <AlertDialog open={confirmDelete !== null} onOpenChange={(v) => !v && setConfirmDelete(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {confirmDelete?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes the secret. Any agent runs that depend on it will
              fail until you add it back.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDelete}
              className="bg-red-500 text-white hover:bg-red-600"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
