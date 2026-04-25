/**
 * P12 — Archive confirm dialog.
 *
 * Soft archive — reversible. Hidden from the default sidebar list, but the
 * row, run history, and artifacts are preserved. The dialog states the
 * reversibility plainly so the user does not feel locked-in.
 */
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
import { Archive } from "lucide-react";

interface ArchiveProjectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectName: string;
  onConfirm: () => void;
  pending?: boolean;
}

export function ArchiveProjectDialog({
  open,
  onOpenChange,
  projectName,
  onConfirm,
  pending = false,
}: ArchiveProjectDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-w-md">
        <AlertDialogHeader>
          <div className="mb-1 flex items-center gap-2 text-muted-foreground">
            <Archive size={14} aria-hidden />
            <span className="text-[11px] font-mono uppercase tracking-[0.16em]">Archive</span>
          </div>
          <AlertDialogTitle className="text-base">
            Archive &ldquo;{projectName}&rdquo;
          </AlertDialogTitle>
          <AlertDialogDescription className="text-xs leading-relaxed text-muted-foreground">
            The run is hidden from your projects list. History, artifacts, and the preview
            URL stay intact. You can restore it anytime from the archive view.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel className="text-xs">Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            disabled={pending}
            className="text-xs"
          >
            {pending ? "Archiving…" : "Archive"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
