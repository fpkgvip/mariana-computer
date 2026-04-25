/**
 * P12 — Share read-only dialog.
 *
 * Reveals a public read-only link to the run (timeline + final answer + preview
 * artifact). Vault secrets and prompt history that contain $KEY references
 * stay redacted on the public view — that promise is stated in the dialog so
 * the user knows what gets shared before they copy.
 *
 * Pure presentation — the parent owns the URL and revocation state.
 */
import { useEffect, useRef, useState } from "react";
import { Check, Copy, Eye, ShieldCheck } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface ShareProjectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectName: string;
  shareUrl: string;
  onRevoke?: () => void;
  revoking?: boolean;
}

export function ShareProjectDialog({
  open,
  onOpenChange,
  projectName,
  shareUrl,
  onRevoke,
  revoking = false,
}: ShareProjectDialogProps) {
  const [copied, setCopied] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // When the dialog opens, focus the URL input and select its content so the
  // user can copy with Cmd/Ctrl+C without an extra click. Reset the copied
  // state on each open so the icon reverts cleanly.
  useEffect(() => {
    if (!open) {
      setCopied(false);
      return;
    }
    const t = window.setTimeout(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    }, 50);
    return () => window.clearTimeout(t);
  }, [open]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API can fail in restricted contexts. Fall back to selecting
      // the input so the user can copy manually with the keyboard.
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <div className="mb-1 flex items-center gap-2 text-muted-foreground">
            <Eye size={14} aria-hidden />
            <span className="text-[11px] font-mono uppercase tracking-[0.16em]">Share read-only</span>
          </div>
          <DialogTitle className="text-base">{projectName}</DialogTitle>
          <DialogDescription className="text-xs leading-relaxed text-muted-foreground">
            Anyone with this link can view the timeline, final answer, and preview. They
            cannot rerun, edit, or see your prompt history.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <input
              ref={inputRef}
              type="text"
              readOnly
              value={shareUrl}
              aria-label="Read-only share link"
              className="flex-1 rounded-md border border-border bg-input px-2.5 py-1.5 font-mono text-xs text-foreground outline-none focus:border-accent"
            />
            <button
              type="button"
              onClick={handleCopy}
              aria-label="Copy share link"
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-background px-2.5 text-xs text-foreground transition-colors hover:bg-secondary"
            >
              {copied ? (
                <>
                  <Check size={12} aria-hidden /> Copied
                </>
              ) : (
                <>
                  <Copy size={12} aria-hidden /> Copy
                </>
              )}
            </button>
          </div>

          <div className="flex items-start gap-2 rounded-md border border-border/60 bg-surface-1/40 px-2.5 py-2 text-[11px] text-muted-foreground">
            <ShieldCheck size={12} className="mt-0.5 shrink-0" aria-hidden />
            <p className="leading-relaxed">
              Vault secrets stay redacted on the public view. <span className="font-mono">$KEY</span>{" "}
              references render as a sentinel.
            </p>
          </div>
        </div>

        <DialogFooter className="flex flex-row items-center justify-between gap-2 sm:justify-between">
          {onRevoke ? (
            <button
              type="button"
              onClick={onRevoke}
              disabled={revoking}
              className="text-[11px] text-muted-foreground underline-offset-2 hover:text-destructive hover:underline disabled:opacity-50"
            >
              {revoking ? "Revoking…" : "Revoke link"}
            </button>
          ) : (
            <span aria-hidden />
          )}
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-accent-foreground transition-opacity hover:opacity-90"
          >
            Done
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
