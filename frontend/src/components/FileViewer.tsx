import { useState, useEffect, useCallback } from "react";
import { X, Download, FileText, Image, FileCode, Table, File, Video, FileSpreadsheet, Presentation, type LucideIcon } from "lucide-react";
import { supabase } from "@/lib/supabase";

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

export interface FileAttachment {
  filename: string;
  size: number;
  mime?: string;
  taskId: string;
}

interface FileViewerProps {
  file: FileAttachment | null;
  onClose: () => void;
  apiUrl: string;
}

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

function getFileExtension(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot >= 0 ? filename.slice(dot + 1).toLowerCase() : "";
}

function getFileTypeIcon(filename: string): LucideIcon {
  const ext = getFileExtension(filename);
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return Image;
  if (["mp4", "webm", "mov"].includes(ext)) return Video;
  if (["csv"].includes(ext)) return Table;
  if (["xlsx", "xls"].includes(ext)) return FileSpreadsheet;
  if (["pptx", "ppt"].includes(ext)) return Presentation;
  if (["json", "html", "xml", "yaml", "yml"].includes(ext)) return FileCode;
  if (["md", "txt", "pdf", "docx", "doc"].includes(ext)) return FileText;
  return File;
}

function getMimeType(filename: string): string {
  const ext = getFileExtension(filename);
  const mimeMap: Record<string, string> = {
    pdf: "application/pdf",
    md: "text/markdown",
    html: "text/html",
    csv: "text/csv",
    json: "application/json",
    txt: "text/plain",
    png: "image/png",
    jpg: "image/jpeg",
    jpeg: "image/jpeg",
    gif: "image/gif",
    webp: "image/webp",
    svg: "image/svg+xml",
    xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    mp4: "video/mp4",
    webm: "video/webm",
    mov: "video/mp4",
  };
  return mimeMap[ext] || "application/octet-stream";
}

/* ------------------------------------------------------------------ */
/*  Inline renderers                                                  */
/* ------------------------------------------------------------------ */

// FE-CRIT-04 fix: Placeholder tokens for code blocks. Code blocks are extracted
// FIRST, replaced with opaque tokens, then markdown transforms run on the
// remaining text. This prevents regex transforms (bold/italic/link) from
// modifying content inside code blocks — which was the XSS/corruption vector.
const _FV_PRE_OPEN = "\u0000FV_PRE\u0000";
const _FV_PRE_CLOSE = "\u0000FV_END\u0000";

function renderMarkdownContent(text: string): string {
  // Step 1: HTML-escape ALL user content first
  let html = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

  // Step 2: Extract fenced code blocks into opaque tokens
  const preservedBlocks: string[] = [];
  if (html.includes("```")) {
    const parts = html.split("```");
    html = parts
      .map((part, idx) => {
        if (idx % 2 === 0) return part;
        const newlineIdx = part.indexOf("\n");
        const code = newlineIdx !== -1 ? part.slice(newlineIdx + 1) : part;
        const rendered = `<pre class="my-2 rounded-md bg-zinc-900 px-4 py-3 text-xs leading-relaxed overflow-x-auto"><code>${code.trim()}</code></pre>`;
        preservedBlocks.push(rendered);
        return `${_FV_PRE_OPEN}${preservedBlocks.length - 1}${_FV_PRE_CLOSE}`;
      })
      .join("");
  }

  // Step 3: Apply markdown transforms ONLY to non-code-block content
  html = html
    .replace(/`([^`]{1,200})`/g, '<code class="rounded bg-zinc-800 px-1.5 py-0.5 text-xs">$1</code>')
    .replace(/\*\*([^\n]{1,200})\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*\n]{1,200})\*/g, "<em>$1</em>")
    .replace(/^### (.+)$/gm, '<h3 class="text-sm font-semibold mt-3 mb-1">$1</h3>')
    .replace(/^## (.+)$/gm, '<h2 class="text-base font-semibold mt-4 mb-2">$1</h2>')
    .replace(/^# (.+)$/gm, '<h1 class="text-lg font-bold mt-4 mb-2">$1</h1>')
    .replace(/\n/g, "<br />");

  // Step 4: Restore code blocks after all transforms are complete
  if (preservedBlocks.length > 0) {
    const tokenRe = new RegExp(`${_FV_PRE_OPEN}(\\d+)${_FV_PRE_CLOSE}`, "g");
    html = html.replace(tokenRe, (_m, idx) => preservedBlocks[Number(idx)] || "");
  }

  return html;
}

// P1-FIX-83: RFC-aware CSV field parser — handles quoted fields with commas.
function parseCsvLine(line: string): string[] {
  const fields: string[] = [];
  let current = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"') {
        if (i + 1 < line.length && line[i + 1] === '"') {
          current += '"';
          i++; // skip escaped quote
        } else {
          inQuotes = false;
        }
      } else {
        current += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === ",") {
        fields.push(current.trim());
        current = "";
      } else {
        current += ch;
      }
    }
  }
  fields.push(current.trim());
  return fields;
}

function CsvTable({ content }: { content: string }) {
  const lines = content.trim().split(/\r?\n/);
  if (lines.length === 0) return <p className="text-xs text-muted-foreground">Empty CSV</p>;

  const headers = parseCsvLine(lines[0]);
  const rows = lines.slice(1).filter(l => l.trim()).map(parseCsvLine);

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-xs">
        <thead>
          <tr className="border-b border-border">
            {headers.map((h, i) => (
              <th
                key={i}
                className="px-3 py-2 text-left font-medium text-muted-foreground"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.slice(0, 200).map((row, ri) => (
            <tr key={ri} className="border-b border-border/50 hover:bg-secondary/30">
              {row.map((cell, ci) => (
                <td key={ci} className="px-3 py-1.5 text-foreground">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > 200 && (
        <p className="px-3 py-2 text-xs text-muted-foreground">
          Showing first 200 of {rows.length} rows
        </p>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  FileContent — fetches and renders inline content                  */
/* ------------------------------------------------------------------ */

function FileContent({ file, apiUrl }: { file: FileAttachment; apiUrl: string }) {
  const [content, setContent] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const ext = getFileExtension(file.filename);
  const mime = file.mime || getMimeType(file.filename);
  const isText = ["md", "txt", "json", "csv", "html"].includes(ext);
  const isImage = ["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext);
  const isPdf = ext === "pdf";
  const isVideo = ["mp4", "webm", "mov"].includes(ext);
  const isOffice = ["pptx", "xlsx", "docx"].includes(ext);

  // BUG-R2-S2-08: The cleanup function captured `blobUrl` from the closure at setup
  // time (always null), so blob URLs were never revoked — causing a memory leak for
  // every file preview. Use a local variable to track the URL created in this effect.
  useEffect(() => {
    let cancelled = false;
    let localBlobUrl: string | null = null;

    async function load() {
      setLoading(true);
      setError(null);
      setContent(null);
      setBlobUrl(null);

      const token = await getAccessToken();
      if (!token) {
        setError("Not authenticated");
        setLoading(false);
        return;
      }

      try {
        const res = await fetch(
          `${apiUrl}/api/investigations/${file.taskId}/files/${encodeURIComponent(file.filename)}`,
          { headers: { Authorization: `Bearer ${token}` } }
        );

        if (!res.ok) {
          setError(`Failed to load file (${res.status})`);
          setLoading(false);
          return;
        }

        if (cancelled) return;

        if (isText) {
          const text = await res.text();
          if (!cancelled) setContent(text);
        } else {
          const blob = await res.blob();
          if (!cancelled) {
            localBlobUrl = URL.createObjectURL(blob);
            setBlobUrl(localBlobUrl);
          }
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load file");
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    load();

    return () => {
      cancelled = true;
      if (localBlobUrl) URL.revokeObjectURL(localBlobUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file.taskId, file.filename, apiUrl]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="h-5 w-5 animate-spin rounded-full border-2 border-border border-t-primary" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-center">
        <p className="text-sm text-red-400">{error}</p>
      </div>
    );
  }

  // Markdown
  if (ext === "md" && content != null) {
    return (
      <div
        className="prose prose-invert prose-sm max-w-none px-4 py-3"
        dangerouslySetInnerHTML={{ __html: renderMarkdownContent(content) }}
      />
    );
  }

  // CSV
  if (ext === "csv" && content != null) {
    return <CsvTable content={content} />;
  }

  // JSON
  if (ext === "json" && content != null) {
    let formatted = content;
    try {
      formatted = JSON.stringify(JSON.parse(content), null, 2);
    } catch {
      // Use raw content
    }
    return (
      <pre className="overflow-x-auto px-4 py-3 font-mono text-xs leading-relaxed text-foreground">
        {formatted}
      </pre>
    );
  }

  // Plain text
  if (isText && content != null) {
    if (ext === "html") {
      // P0-FIX-2: Removed allow-same-origin from sandbox to prevent XSS.
      // Uploaded HTML must not share origin with the app.
      return (
        <iframe
          srcDoc={content}
          title={file.filename}
          className="h-[600px] w-full rounded bg-white"
          sandbox=""
        />
      );
    }
    return (
      <pre className="overflow-x-auto whitespace-pre-wrap break-words px-4 py-3 font-mono text-xs leading-relaxed text-foreground">
        {content}
      </pre>
    );
  }

  // PDF — FE-CRIT-05 fix: Sandbox the PDF iframe. PDFs are served via blob URLs
  // (same origin) so allow-same-origin is needed for the PDF renderer to work,
  // but no other permissions (scripts, forms, popups, etc.) are granted.
  if (isPdf && blobUrl) {
    return (
      <iframe
        src={blobUrl}
        title={file.filename}
        className="h-[700px] w-full rounded"
        sandbox="allow-same-origin"
      />
    );
  }

  // Image
  if (isImage && blobUrl) {
    return (
      <div className="flex justify-center p-4">
        <img
          src={blobUrl}
          alt={file.filename}
          className="max-h-[600px] max-w-full rounded object-contain"
        />
      </div>
    );
  }

  // Video
  if (isVideo && blobUrl) {
    return (
      <div className="p-4">
        <video
          controls
          className="w-full max-h-[600px] rounded"
          preload="metadata"
        >
          <source src={blobUrl} type={mime} />
          Your browser does not support video playback.
        </video>
      </div>
    );
  }

  // Office documents (PPTX, XLSX, DOCX) — show info + download prompt
  if (isOffice) {
    const officeLabels: Record<string, { label: string; desc: string }> = {
      pptx: { label: "PowerPoint Presentation", desc: "Download to open in Microsoft PowerPoint or Google Slides." },
      xlsx: { label: "Excel Spreadsheet", desc: "Download to open in Microsoft Excel or Google Sheets." },
      docx: { label: "Word Document", desc: "Download to open in Microsoft Word or Google Docs." },
    };
    const info = officeLabels[ext] || { label: "Office Document", desc: "Download to view this file." };
    const Icon = getFileTypeIcon(file.filename);
    return (
      <div className="flex flex-col items-center justify-center gap-4 py-16">
        <Icon size={48} className="text-muted-foreground/30" />
        <div className="text-center">
          <p className="text-sm font-medium text-foreground">{info.label}</p>
          <p className="mt-1 text-xs text-muted-foreground max-w-xs">{info.desc}</p>
          {file.size > 0 && (
            <p className="mt-1 text-[10px] text-muted-foreground/50">{formatBytes(file.size)}</p>
          )}
        </div>
        <p className="text-xs text-muted-foreground/50">
          Use the Download button above to save this file.
        </p>
      </div>
    );
  }

  // Fallback — download button
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-12">
      <File size={32} className="text-muted-foreground/50" />
      <p className="text-sm text-muted-foreground">Preview not available for this file type</p>
      <p className="text-xs text-muted-foreground/50">{mime}</p>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/*  FileViewer (main export)                                          */
/* ------------------------------------------------------------------ */

export default function FileViewer({ file, onClose, apiUrl }: FileViewerProps) {
  const handleDownload = useCallback(async () => {
    if (!file) return;
    const token = await getAccessToken();
    if (!token) return;

    try {
      const res = await fetch(
        `${apiUrl}/api/investigations/${file.taskId}/files/${encodeURIComponent(file.filename)}`,
        { headers: { Authorization: `Bearer ${token}` } }
      );
      if (!res.ok) return;
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = file.filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 100);
    } catch (err) {
      console.error("[FileViewer] Download error:", err);
    }
  }, [file, apiUrl]);

  if (!file) return null;

  const Icon = getFileTypeIcon(file.filename);

  return (
    <>
      {/* Backdrop */}
      <div className="fixed inset-0 z-50 bg-black/50" onClick={onClose} />

      {/* Slide-over panel */}
      <div className="fixed inset-y-0 right-0 z-50 flex w-full max-w-2xl flex-col border-l border-border bg-card shadow-2xl animate-slide-in-right">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2 min-w-0">
            <Icon size={16} className="shrink-0 text-muted-foreground" />
            <span className="truncate text-sm font-medium text-foreground">
              {file.filename}
            </span>
            <span className="shrink-0 text-xs text-muted-foreground/50">
              {formatBytes(file.size)}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={handleDownload}
              className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1.5 text-xs text-foreground hover:bg-secondary transition-colors"
            >
              <Download size={12} />
              Download
            </button>
            <button
              onClick={onClose}
              className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary transition-colors"
              aria-label="Close file viewer"
            >
              <X size={16} />
            </button>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto">
          <FileContent file={file} apiUrl={apiUrl} />
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  FileCard — compact card shown in chat timeline                    */
/* ------------------------------------------------------------------ */

export function FileCard({
  filename,
  size,
  onClick,
}: {
  filename: string;
  size?: number;
  onClick: () => void;
}) {
  const Icon = getFileTypeIcon(filename);
  const ext = getFileExtension(filename);

  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-2 rounded-md border border-border bg-card/50 px-3 py-2 text-xs transition-colors hover:bg-secondary/50"
    >
      <Icon size={14} className="shrink-0 text-muted-foreground" />
      <span className="truncate text-foreground">{filename}</span>
      {size != null && (
        <span className="shrink-0 text-muted-foreground/50">{formatBytes(size)}</span>
      )}
      <span className="shrink-0 rounded bg-secondary px-1.5 py-0.5 text-[9px] font-medium uppercase text-muted-foreground">
        {ext}
      </span>
    </button>
  );
}
