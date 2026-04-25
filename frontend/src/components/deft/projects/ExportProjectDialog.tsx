/**
 * P12 — Export run dialog.
 *
 * Two formats: a portable .zip (timeline + artifacts + final answer + receipt)
 * and a JSON snapshot for programmatic use. The dialog states what is
 * included and what is omitted so the user understands the boundary —
 * Vault secret values are never exported, only the $KEY sentinel
 * placeholder.
 */
import { useState } from "react";
import { Download, FileArchive, FileJson, ShieldCheck } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";

export type ExportFormat = "zip" | "json";

interface ExportProjectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectName: string;
  onExport: (format: ExportFormat) => void;
  pending?: boolean;
}

export function ExportProjectDialog({
  open,
  onOpenChange,
  projectName,
  onExport,
  pending = false,
}: ExportProjectDialogProps) {
  const [format, setFormat] = useState<ExportFormat>("zip");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <div className="mb-1 flex items-center gap-2 text-muted-foreground">
            <Download size={14} aria-hidden />
            <span className="text-[11px] font-mono uppercase tracking-[0.16em]">Export run</span>
          </div>
          <DialogTitle className="text-base">{projectName}</DialogTitle>
          <DialogDescription className="text-xs leading-relaxed text-muted-foreground">
            Download the timeline, artifacts, final answer, and receipt. Vault values
            stay redacted as <span className="font-mono">$KEY</span> sentinels.
          </DialogDescription>
        </DialogHeader>

        <fieldset className="space-y-2">
          <legend className="sr-only">Export format</legend>
          <FormatRadio
            id="export-zip"
            checked={format === "zip"}
            onChange={() => setFormat("zip")}
            icon={<FileArchive size={14} aria-hidden />}
            title="Archive (.zip)"
            subtitle="timeline + artifacts + receipt + final answer"
          />
          <FormatRadio
            id="export-json"
            checked={format === "json"}
            onChange={() => setFormat("json")}
            icon={<FileJson size={14} aria-hidden />}
            title="Snapshot (.json)"
            subtitle="machine-readable run metadata, no binaries"
          />
        </fieldset>

        <div className="flex items-start gap-2 rounded-md border border-border/60 bg-surface-1/40 px-2.5 py-2 text-[11px] text-muted-foreground">
          <ShieldCheck size={12} className="mt-0.5 shrink-0" aria-hidden />
          <p className="leading-relaxed">
            Secret values never leave Deft. Only the sentinel name is exported.
          </p>
        </div>

        <DialogFooter>
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            className="rounded-md border border-border bg-background px-3 py-1.5 text-xs text-foreground transition-colors hover:bg-secondary"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onExport(format)}
            disabled={pending}
            className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-accent-foreground transition-opacity hover:opacity-90 disabled:opacity-60"
          >
            {pending ? "Preparing…" : format === "zip" ? "Download .zip" : "Download .json"}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function FormatRadio({
  id,
  checked,
  onChange,
  icon,
  title,
  subtitle,
}: {
  id: string;
  checked: boolean;
  onChange: () => void;
  icon: React.ReactNode;
  title: string;
  subtitle: string;
}) {
  return (
    <label
      htmlFor={id}
      className={cn(
        "flex cursor-pointer items-start gap-2.5 rounded-md border px-3 py-2.5 transition-colors",
        checked
          ? "border-accent bg-accent/5"
          : "border-border bg-background hover:bg-secondary/40",
      )}
    >
      <input
        id={id}
        type="radio"
        name="export-format"
        checked={checked}
        onChange={onChange}
        className="sr-only"
      />
      <span
        aria-hidden
        className={cn(
          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border",
          checked ? "border-accent" : "border-muted-foreground",
        )}
      >
        {checked ? <span className="h-2 w-2 rounded-full bg-accent" /> : null}
      </span>
      <span className="flex-1 leading-tight">
        <span className="flex items-center gap-1.5 text-xs font-medium text-foreground">
          {icon}
          {title}
        </span>
        <span className="mt-0.5 block text-[11px] text-muted-foreground">{subtitle}</span>
      </span>
    </label>
  );
}
