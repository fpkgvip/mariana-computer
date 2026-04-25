/**
 * SecretsTableView — pure presentation of the vault secrets table.
 *
 * Holds NO crypto state.  All data and callbacks are injected by the parent
 * (production wrapper = SecretsTable; preview = DevVault).
 *
 * Polish over the legacy table:
 *   - Masked preview rendered as a monospaced • chip instead of inline text
 *     (no more accidental "····undefined" frame between decrypts)
 *   - A $KEY copy chip per row that copies the literal sentinel users paste
 *     into prompts ($OPENAI_API_KEY).  Distinct from "copy plaintext".
 *   - Empty/zero rows handled by parent (this view always has ≥1 secret).
 */
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
import { Pencil, Trash2, Copy, Check, Hash, KeyRound } from "lucide-react";
import { useState } from "react";
import type { SecretDTO } from "@/lib/vaultApi";

export interface SecretsTableViewProps {
  secrets: SecretDTO[];
  /** Decrypted preview tail keyed by secret id ("4 chars only" usually). */
  previews: Record<string, string>;
  onCopyValue: (s: SecretDTO) => Promise<void> | void;
  onCopySentinel: (s: SecretDTO) => Promise<void> | void;
  onEdit: (s: SecretDTO) => void;
  onDelete: (s: SecretDTO) => void;
  onAdd: () => void;
}

function formatRelative(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return "moments ago";
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

export function SecretsTableView(props: SecretsTableViewProps) {
  const { secrets, previews, onCopyValue, onCopySentinel, onEdit, onDelete, onAdd } = props;
  const [copiedId, setCopiedId] = useState<string | null>(null);
  const [copiedSentinelId, setCopiedSentinelId] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SecretDTO | null>(null);

  const handleCopy = async (s: SecretDTO) => {
    await onCopyValue(s);
    setCopiedId(s.id);
    window.setTimeout(() => setCopiedId(null), 1500);
  };
  const handleSentinel = async (s: SecretDTO) => {
    await onCopySentinel(s);
    setCopiedSentinelId(s.id);
    window.setTimeout(() => setCopiedSentinelId(null), 1500);
  };

  return (
    <>
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          {secrets.length} secret{secrets.length === 1 ? "" : "s"}
        </p>
        <Button onClick={onAdd} size="sm">
          <KeyRound size={13} className="mr-1.5" aria-hidden />
          Add secret
        </Button>
      </div>

      {/* Desktop: table layout (≥1024px). Tablet/mobile use cards below. */}
      <div className="hidden overflow-hidden rounded-xl border border-border/70 lg:block">
        <table className="w-full text-sm">
          <thead className="bg-surface-1/60 text-[11px] uppercase tracking-[0.12em] text-muted-foreground">
            <tr>
              <th className="px-4 py-2.5 text-left font-medium">Name</th>
              <th className="px-4 py-2.5 text-left font-medium">Preview</th>
              <th className="px-4 py-2.5 text-left font-medium">Description</th>
              <th className="px-4 py-2.5 text-left font-medium">Updated</th>
              <th className="px-4 py-2.5 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody>
            {secrets.map((s) => {
              const tail = previews[s.id];
              return (
                <tr
                  key={s.id}
                  className="border-t border-border/70 align-middle hover:bg-surface-1/30"
                >
                  <td className="px-4 py-3 font-mono text-[12.5px] text-foreground">
                    <div className="flex items-center gap-2">
                      <span>{s.name}</span>
                      <button
                        type="button"
                        onClick={() => void handleSentinel(s)}
                        title={`Copy $${s.name} for use in prompts`}
                        aria-label={`Copy $${s.name} sentinel`}
                        className="inline-flex items-center gap-1 rounded-md border border-border/60 bg-surface-1/60 px-1.5 py-0.5 text-[10.5px] text-muted-foreground transition-colors hover:border-border hover:text-foreground"
                      >
                        {copiedSentinelId === s.id ? (
                          <Check size={10} aria-hidden />
                        ) : (
                          <Hash size={10} aria-hidden />
                        )}
                        ${s.name}
                      </button>
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className="inline-flex items-center gap-1 rounded-md bg-muted/60 px-2 py-0.5 font-mono text-[11.5px] tracking-wider text-muted-foreground">
                      <span aria-hidden>••••</span>
                      <span>{tail ?? "····"}</span>
                    </span>
                  </td>
                  <td className="px-4 py-3 text-muted-foreground">
                    <span className="line-clamp-1">{s.description ?? "—"}</span>
                  </td>
                  <td className="px-4 py-3 text-[12px] text-muted-foreground">
                    {formatRelative(s.updated_at)}
                  </td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-1">
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={() => void handleCopy(s)}
                        aria-label={`Copy plaintext value of ${s.name}`}
                        title="Copy plaintext"
                      >
                        {copiedId === s.id ? <Check size={14} /> : <Copy size={14} />}
                      </Button>
                      <Button
                        size="icon"
                        variant="ghost"
                        onClick={() => onEdit(s)}
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
                        className="text-rose-300 hover:bg-rose-500/10 hover:text-rose-200"
                      >
                        <Trash2 size={14} />
                      </Button>
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Tablet + mobile: card list (table is too dense below 1024px). */}
      <ul className="space-y-2 lg:hidden">
        {secrets.map((s) => {
          const tail = previews[s.id];
          return (
            <li
              key={s.id}
              className="rounded-xl border border-border/70 bg-surface-1/40 p-3"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate font-mono text-[13px] text-foreground">{s.name}</p>
                  {s.description && (
                    <p className="mt-0.5 line-clamp-2 text-[12px] text-muted-foreground">
                      {s.description}
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px]">
                    <span className="inline-flex items-center gap-1 rounded-md bg-muted/60 px-2 py-0.5 font-mono tracking-wider text-muted-foreground">
                      <span aria-hidden>••••</span>
                      <span>{tail ?? "····"}</span>
                    </span>
                    <span className="text-muted-foreground">
                      {formatRelative(s.updated_at)}
                    </span>
                  </div>
                </div>
                <div className="flex gap-1">
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() => void handleCopy(s)}
                    aria-label={`Copy plaintext value of ${s.name}`}
                  >
                    {copiedId === s.id ? <Check size={14} /> : <Copy size={14} />}
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() => onEdit(s)}
                    aria-label={`Edit ${s.name}`}
                  >
                    <Pencil size={14} />
                  </Button>
                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={() => setConfirmDelete(s)}
                    aria-label={`Delete ${s.name}`}
                    className="text-rose-300 hover:bg-rose-500/10 hover:text-rose-200"
                  >
                    <Trash2 size={14} />
                  </Button>
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      <AlertDialog
        open={confirmDelete !== null}
        onOpenChange={(v) => !v && setConfirmDelete(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {confirmDelete?.name}?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently removes the secret. Any agent runs that depend on it
              will fail until you add it back.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (confirmDelete) onDelete(confirmDelete);
                setConfirmDelete(null);
              }}
              className="bg-rose-500 text-white hover:bg-rose-600"
            >
              Delete
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
