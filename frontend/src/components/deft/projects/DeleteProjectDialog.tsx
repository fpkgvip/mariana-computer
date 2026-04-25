/**
 * P12 — Delete confirm dialog.
 *
 * Permanent. Distinguished from Archive by the "this cannot be undone" line
 * and a destructive-styled action. The user must confirm — there is no
 * undo toast for this surface in v1.
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
import { Trash2 } from "lucide-react";

interface DeleteProjectDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectName: string;
  onConfirm: () => void;
  pending?: boolean;
}

export function DeleteProjectDialog({
  open,
  onOpenChange,
  projectName,
  onConfirm,
  pending = false,
}: DeleteProjectDialogProps) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-w-md">
        <AlertDialogHeader>
          <div className="mb-1 flex items-center gap-2 text-destructive">
            <Trash2 size={14} aria-hidden />
            <span className="text-[11px] font-mono uppercase tracking-[0.16em]">Delete</span>
          </div>
          <AlertDialogTitle className="text-base">
            Delete &ldquo;{projectName}&rdquo;
          </AlertDialogTitle>
          <AlertDialogDescription className="text-xs leading-relaxed text-muted-foreground">
            The run, its timeline, and its artifacts are removed. The preview URL stops
            resolving. This cannot be undone — archive instead if you might want it back.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel className="text-xs">Cancel</AlertDialogCancel>
          <AlertDialogAction
            onClick={onConfirm}
            disabled={pending}
            className="bg-destructive text-xs text-destructive-foreground hover:bg-destructive/90"
          >
            {pending ? "Deleting…" : "Delete permanently"}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
