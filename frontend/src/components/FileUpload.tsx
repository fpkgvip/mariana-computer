import { useState, useRef, useCallback, DragEvent } from "react";
import { Paperclip, X, FileText, Image, Table, FileCode, File, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { supabase } from "@/lib/supabase";

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

export interface UploadedFile {
  name: string;
  size: number;
  type: string;
  progress: number; // 0-100
  error?: string;
}

export interface UploadSession {
  session_uuid: string;
  files: UploadedFile[];
}

interface FileUploadProps {
  uploadedFiles: UploadedFile[];
  onFilesChange: (files: UploadedFile[]) => void;
  sessionUuid: string | null;
  onSessionUuid: (uuid: string) => void;
  disabled?: boolean;
  apiUrl: string;
}

/* ------------------------------------------------------------------ */
/*  Constants                                                         */
/* ------------------------------------------------------------------ */

const MAX_FILE_SIZE = 10 * 1024 * 1024; // 10MB
const MAX_FILES = 5;
const ACCEPTED_EXTENSIONS = [
  ".pdf", ".txt", ".md", ".csv", ".json", ".html",
  ".png", ".jpg", ".jpeg", ".xlsx", ".docx",
];
const ACCEPTED_MIME_TYPES = [
  "application/pdf", "text/plain", "text/markdown", "text/csv",
  "application/json", "text/html", "image/png", "image/jpeg",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
];

/* ------------------------------------------------------------------ */
/*  Helpers                                                           */
/* ------------------------------------------------------------------ */

async function getAccessToken(): Promise<string | null> {
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function getFileExtension(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

function getFileIcon(name: string): React.FC<{ size?: number; className?: string }> {
  const ext = getFileExtension(name);
  if ([".png", ".jpg", ".jpeg"].includes(ext)) return Image;
  if ([".csv", ".xlsx"].includes(ext)) return Table;
  if ([".json", ".html"].includes(ext)) return FileCode;
  if ([".md", ".txt", ".pdf", ".docx"].includes(ext)) return FileText;
  return File;
}

function isValidFile(file: globalThis.File): string | null {
  const ext = getFileExtension(file.name);
  if (!ACCEPTED_EXTENSIONS.includes(ext)) {
    return `Unsupported file type: ${ext || "unknown"}`;
  }
  // FE-CRIT-08 fix: Actually check the MIME type. The ACCEPTED_MIME_TYPES
  // constant was defined but never used in validation, making it dead code.
  // Reject files whose MIME type doesn't match the allowed list (unless the
  // browser reports an empty string, which happens for some valid file types).
  if (file.type && !ACCEPTED_MIME_TYPES.includes(file.type)) {
    return `Unsupported MIME type: ${file.type}`;
  }
  if (file.size > MAX_FILE_SIZE) {
    return `File too large (${formatBytes(file.size)}). Maximum is 10 MB.`;
  }
  return null;
}

/* ------------------------------------------------------------------ */
/*  FileUpload component                                              */
/* ------------------------------------------------------------------ */

export default function FileUpload({
  uploadedFiles,
  onFilesChange,
  sessionUuid,
  onSessionUuid,
  disabled = false,
  apiUrl,
}: FileUploadProps) {
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragCounterRef = useRef(0);

  // BUG-R2-S2-09 + BUG-C3-03 fix: Use a local accumulator ref that is updated
  // synchronously on each uploadFile call, so concurrent uploads in the same tick
  // don't overwrite each other.  The ref is synced FROM React state on re-render
  // AND updated optimistically in uploadFile before onFilesChange fires.
  const uploadedFilesRef = useRef(uploadedFiles);
  uploadedFilesRef.current = uploadedFiles;

  const uploadFile = useCallback(
    async (file: globalThis.File) => {
      const token = await getAccessToken();
      if (!token) {
        toast.error("Not authenticated");
        return;
      }

      // Add file to state with 0 progress
      const newFile: UploadedFile = {
        name: file.name,
        size: file.size,
        type: file.type,
        progress: 0,
      };

      // BUG-C3-03 fix: Synchronously push into the ref BEFORE calling
      // onFilesChange so the next concurrent uploadFile call sees it.
      const snapshot = [...uploadedFilesRef.current, newFile];
      uploadedFilesRef.current = snapshot;
      onFilesChange(snapshot);

      const formData = new FormData();
      formData.append("files", file);
      if (sessionUuid) {
        formData.append("session_uuid", sessionUuid);
      }

      try {
        const xhr = new XMLHttpRequest();

        const uploadPromise = new Promise<{ session_uuid: string }>((resolve, reject) => {
          xhr.upload.addEventListener("progress", (e) => {
            if (e.lengthComputable) {
              const pct = Math.round((e.loaded / e.total) * 100);
              // Use ref to get latest files and update progress for this specific file
              const current = uploadedFilesRef.current;
              const idx = current.findIndex((f) => f.name === file.name && f.size === file.size);
              if (idx >= 0) {
                const updated = [...current];
                updated[idx] = { ...updated[idx], progress: pct };
                onFilesChange(updated);
              }
            }
          });

          xhr.addEventListener("load", () => {
            if (xhr.status >= 200 && xhr.status < 300) {
              try {
                resolve(JSON.parse(xhr.responseText));
              } catch {
                reject(new Error("Invalid response"));
              }
            } else {
              reject(new Error(`Upload failed (${xhr.status})`));
            }
          });

          xhr.addEventListener("error", () => reject(new Error("Network error")));
          xhr.addEventListener("abort", () => reject(new Error("Upload cancelled")));

          // BUG-R3-01: Endpoint is /api/upload — the backend defines the pending
          // upload route at POST /api/upload (api.py line 1556), not /api/uploads/pending.
          // The BUG-R2-S2-03 fix incorrectly changed this, breaking all file uploads.
          xhr.open("POST", `${apiUrl}/api/upload`);
          xhr.setRequestHeader("Authorization", `Bearer ${token}`);
          xhr.send(formData);
        });

        const result = await uploadPromise;

        if (result.session_uuid && !sessionUuid) {
          onSessionUuid(result.session_uuid);
        }

        // Mark as complete using ref for latest state
        const current = uploadedFilesRef.current;
        const idx = current.findIndex((f) => f.name === file.name && f.size === file.size);
        if (idx >= 0) {
          const updated = [...current];
          updated[idx] = { ...updated[idx], progress: 100 };
          onFilesChange(updated);
        }
      } catch (err) {
        const errorMsg = err instanceof Error ? err.message : "Upload failed";
        const current = uploadedFilesRef.current;
        const idx = current.findIndex((f) => f.name === file.name && f.size === file.size);
        if (idx >= 0) {
          const updated = [...current];
          updated[idx] = { ...updated[idx], progress: 0, error: errorMsg };
          onFilesChange(updated);
        }
        toast.error(`Failed to upload ${file.name}`, { description: errorMsg });
      }
    },
    [sessionUuid, onFilesChange, onSessionUuid, apiUrl]
  );

  const handleFiles = useCallback(
    (fileList: FileList | null) => {
      if (!fileList || fileList.length === 0) return;

      const currentCount = uploadedFiles.length;
      const available = MAX_FILES - currentCount;

      if (available <= 0) {
        toast.error(`Maximum ${MAX_FILES} files allowed`);
        return;
      }

      const filesToUpload = Array.from(fileList).slice(0, available);

      for (const file of filesToUpload) {
        const error = isValidFile(file);
        if (error) {
          toast.error(error);
          continue;
        }
        uploadFile(file);
      }

      if (fileList.length > available) {
        toast.warning(`Only ${available} more file${available !== 1 ? "s" : ""} allowed`);
      }
    },
    [uploadedFiles, uploadFile]
  );

  const removeFile = useCallback(
    (index: number) => {
      onFilesChange(uploadedFiles.filter((_, i) => i !== index));
    },
    [uploadedFiles, onFilesChange]
  );

  /* Drag handlers */
  const handleDragEnter = useCallback((e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current++;
    if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
      setIsDragging(true);
    }
  }, []);

  const handleDragLeave = useCallback((e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    dragCounterRef.current--;
    if (dragCounterRef.current === 0) {
      setIsDragging(false);
    }
  }, []);

  const handleDragOver = useCallback((e: DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDrop = useCallback(
    (e: DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);
      dragCounterRef.current = 0;
      handleFiles(e.dataTransfer.files);
    },
    [handleFiles]
  );

  const acceptString = ACCEPTED_EXTENSIONS.join(",");

  return (
    <div
      onDragEnter={handleDragEnter}
      onDragLeave={handleDragLeave}
      onDragOver={handleDragOver}
      onDrop={handleDrop}
    >
      {/* Drop zone overlay */}
      {isDragging && (
        <div className="mb-2 rounded-md border-2 border-dashed border-primary/40 bg-primary/5 px-4 py-6 text-center">
          <p className="text-xs text-primary">Drop files here</p>
        </div>
      )}

      {/* Uploaded file previews */}
      {uploadedFiles.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-2">
          {uploadedFiles.map((uf, idx) => {
            const Icon = getFileIcon(uf.name);
            return (
              <div
                key={`${uf.name}-${idx}`}
                className="relative flex items-center gap-2 rounded-md border border-border bg-card/50 px-2.5 py-1.5 text-xs"
              >
                <Icon size={13} className="shrink-0 text-muted-foreground" />
                <span className="max-w-[120px] truncate text-foreground">{uf.name}</span>
                <span className="text-muted-foreground/50">{formatBytes(uf.size)}</span>

                {/* Progress bar */}
                {uf.progress > 0 && uf.progress < 100 && !uf.error && (
                  <div className="absolute bottom-0 left-0 h-0.5 w-full overflow-hidden rounded-b-md">
                    <div
                      className="h-full bg-primary transition-all duration-200"
                      style={{ width: `${uf.progress}%` }}
                    />
                  </div>
                )}

                {/* Uploading spinner */}
                {uf.progress > 0 && uf.progress < 100 && !uf.error && (
                  <Loader2 size={11} className="animate-spin text-primary" />
                )}

                {/* Error indicator */}
                {uf.error && (
                  <span className="text-[9px] text-red-400" title={uf.error}>
                    Error
                  </span>
                )}

                {/* Remove button */}
                {!disabled && (
                  <button
                    onClick={() => removeFile(idx)}
                    className="shrink-0 rounded p-0.5 text-muted-foreground hover:text-foreground transition-colors"
                    aria-label={`Remove ${uf.name}`}
                  >
                    <X size={11} />
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={acceptString}
        onChange={(e) => {
          handleFiles(e.target.files);
          e.target.value = ""; // Reset so same file can be re-selected
        }}
        className="hidden"
        disabled={disabled}
      />

      {/* Paperclip button — exposed for external use */}
      <button
        type="button"
        onClick={() => fileInputRef.current?.click()}
        disabled={disabled || uploadedFiles.length >= MAX_FILES}
        className="flex shrink-0 items-center justify-center rounded-md p-2 text-muted-foreground transition-colors hover:text-foreground hover:bg-secondary disabled:opacity-40"
        aria-label="Attach files"
        title={uploadedFiles.length >= MAX_FILES ? `Maximum ${MAX_FILES} files` : "Attach files"}
      >
        <Paperclip size={16} />
      </button>
    </div>
  );
}
