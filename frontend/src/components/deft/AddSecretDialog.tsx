/**
 * AddSecretDialog — modal for creating or editing a vault secret.
 */
import { useEffect, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useVault } from "@/hooks/useVault";
import { Eye, EyeOff, Loader2 } from "lucide-react";
import { toast } from "sonner";
import type { SecretDTO } from "@/lib/vaultApi";

interface Props {
  open: boolean;
  onClose: () => void;
  /** When set, edit existing secret (name field is read-only). */
  editing?: SecretDTO;
}

const NAME_RE = /^[A-Z][A-Z0-9_]{0,63}$/;

export function AddSecretDialog({ open, onClose, editing }: Props) {
  const vault = useVault();
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [description, setDescription] = useState("");
  const [showValue, setShowValue] = useState(false);
  const [busy, setBusy] = useState(false);

  // Reset form whenever the dialog opens or the editing target changes.
  useEffect(() => {
    if (!open) return;
    setName(editing?.name ?? "");
    setValue("");
    setDescription(editing?.description ?? "");
    setShowValue(false);
    setBusy(false);
  }, [open, editing]);

  const isEditing = editing !== undefined;
  const nameError =
    !isEditing && name.length > 0 && !NAME_RE.test(name)
      ? "Use UPPER_SNAKE_CASE (A–Z, 0–9, underscore). Must start with a letter, max 64 chars."
      : null;
  const valueError =
    value.length > 0 && value.length > 16384 ? "Value too long (max 16384 chars)." : null;

  const canSubmit =
    !busy &&
    value.length > 0 &&
    (isEditing || NAME_RE.test(name)) &&
    !valueError;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    try {
      if (isEditing && editing) {
        await vault.updateSecret(editing.id, value, description.trim() || undefined);
        toast.success(`Updated ${editing.name}.`);
      } else {
        await vault.addSecret(name, value, description.trim() || undefined);
        toast.success(`Saved ${name}.`);
      }
      // Clear plaintext from memory.
      setValue("");
      onClose();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast.error(msg);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => !v && !busy && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{isEditing ? `Edit ${editing?.name}` : "Add secret"}</DialogTitle>
        </DialogHeader>

        <div className="space-y-4 text-sm">
          {!isEditing && (
            <div className="space-y-2">
              <Label htmlFor="secret-name">Name</Label>
              <Input
                id="secret-name"
                value={name}
                onChange={(e) => setName(e.target.value.toUpperCase())}
                placeholder="OPENAI_API_KEY"
                autoComplete="off"
                spellCheck={false}
                className="font-mono"
              />
              {nameError && <p className="text-xs text-red-400">{nameError}</p>}
              <p className="text-xs text-muted-foreground">
                Reference in prompts as <code className="rounded bg-muted px-1">$NAME</code>.
              </p>
            </div>
          )}

          <div className="space-y-2">
            <Label htmlFor="secret-value">{isEditing ? "New value" : "Value"}</Label>
            <div className="relative">
              <Input
                id="secret-value"
                type={showValue ? "text" : "password"}
                value={value}
                onChange={(e) => setValue(e.target.value)}
                autoComplete="off"
                spellCheck={false}
                placeholder="sk-…"
              />
              <button
                type="button"
                onClick={() => setShowValue((v) => !v)}
                className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground hover:text-foreground"
                aria-label={showValue ? "Hide value" : "Show value"}
              >
                {showValue ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </div>
            {valueError && <p className="text-xs text-red-400">{valueError}</p>}
          </div>

          <div className="space-y-2">
            <Label htmlFor="secret-desc">Description (optional)</Label>
            <Input
              id="secret-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this is for"
              maxLength={256}
            />
          </div>

          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button onClick={handleSubmit} disabled={!canSubmit}>
              {busy ? <Loader2 size={14} className="mr-2 animate-spin" /> : null}
              {isEditing ? "Save" : "Add secret"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
