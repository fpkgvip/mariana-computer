/**
 * P12 — Kebab menu for a project row.
 *
 * Renders a 3-dot trigger that opens a dropdown with the four reversible
 * actions: archive, share read-only, export, delete. The menu surfaces
 * the action — confirmation dialogs handle the actual side effect.
 *
 * Pure presentation. No data fetching. The parent decides whether the
 * row is archived (so we can render "Restore" instead of "Archive").
 */
import { Archive, ArchiveRestore, Download, MoreHorizontal, Share2, Trash2 } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

interface ProjectRowMenuProps {
  archived: boolean;
  onArchive: () => void;
  onRestore: () => void;
  onShare: () => void;
  onExport: () => void;
  onDelete: () => void;
  /** Optional: pre-open the menu — used by the dev preview to capture a screenshot. */
  open?: boolean;
  /** Optional label — improves screen-reader context. Default "Project actions". */
  label?: string;
}

export function ProjectRowMenu({
  archived,
  onArchive,
  onRestore,
  onShare,
  onExport,
  onDelete,
  open,
  label = "Project actions",
}: ProjectRowMenuProps) {
  return (
    <DropdownMenu open={open}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={label}
          onClick={(e) => {
            // Stop propagation so the row click handler does not fire.
            e.stopPropagation();
          }}
          className={cn(
            "inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-foreground",
            "opacity-0 transition-opacity hover:bg-secondary hover:text-foreground",
            "group-hover:opacity-100 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent",
            "data-[state=open]:opacity-100 data-[state=open]:bg-secondary data-[state=open]:text-foreground",
          )}
        >
          <MoreHorizontal size={14} aria-hidden />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        side="bottom"
        sideOffset={4}
        className="w-44"
        onClick={(e) => e.stopPropagation()}
      >
        {archived ? (
          <DropdownMenuItem onSelect={onRestore} className="gap-2 text-xs">
            <ArchiveRestore size={12} aria-hidden /> Restore
          </DropdownMenuItem>
        ) : (
          <DropdownMenuItem onSelect={onArchive} className="gap-2 text-xs">
            <Archive size={12} aria-hidden /> Archive
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onSelect={onShare} className="gap-2 text-xs">
          <Share2 size={12} aria-hidden /> Share read-only
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onExport} className="gap-2 text-xs">
          <Download size={12} aria-hidden /> Export run
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          onSelect={onDelete}
          className="gap-2 text-xs text-destructive focus:text-destructive"
        >
          <Trash2 size={12} aria-hidden /> Delete
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
