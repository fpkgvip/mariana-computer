import { useCallback, useEffect, useState } from "react";

interface WorkspaceEntry {
  path: string;
  type: string;
  size: number;
  mtime?: number;
}

interface WorkspaceFile {
  path: string;
  name: string;
  size: number;
  modified?: number;
  type?: string;
}

interface WorkspaceResponse {
  root: string;
  entries: WorkspaceEntry[];
  truncated?: boolean;
}

export interface WorkspaceSidebarProps {
  apiUrl: string;
  getToken: () => Promise<string | null>;
  userId: string;
  refreshTick?: number;
}

const formatSize = (bytes: number) => {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};

/**
 * Sidebar listing files in the user's persistent workspace dir,
 * with one-click download (blob fetch → object URL).
 */
export function WorkspaceSidebar({ apiUrl, getToken, userId, refreshTick = 0 }: WorkspaceSidebarProps) {
  const [files, setFiles] = useState<WorkspaceFile[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const token = await getToken();
      const res = await fetch(`${apiUrl}/api/workspace/${encodeURIComponent(userId)}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) {
        throw new Error(`${res.status} ${res.statusText}`);
      }
      const data: WorkspaceResponse = await res.json();
      const rawEntries = Array.isArray(data.entries) ? data.entries : [];
      const list: WorkspaceFile[] = rawEntries
        .filter((e) => e.type === "file")
        .map((e) => ({
          path: e.path,
          name: e.path.split("/").pop() || e.path,
          size: e.size,
          modified: e.mtime,
          type: e.type,
        }));
      list.sort((a, b) => (b.modified ?? 0) - (a.modified ?? 0));
      setFiles(list);
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, [apiUrl, getToken, userId]);

  useEffect(() => {
    load();
  }, [load, refreshTick]);

  const handleDownload = async (file: WorkspaceFile) => {
    try {
      const token = await getToken();
      const res = await fetch(
        `${apiUrl}/api/workspace/${encodeURIComponent(userId)}/file?path=${encodeURIComponent(file.path)}`,
        { headers: token ? { Authorization: `Bearer ${token}` } : {} },
      );
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = file.name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setErr(`Download failed: ${String(e)}`);
    }
  };

  return (
    <aside className="rounded-xl border border-white/10 bg-black/30 backdrop-blur p-3 h-fit sticky top-4">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-xs font-semibold uppercase tracking-wider text-white/70">Workspace</h4>
        <button
          onClick={load}
          className="text-[11px] text-white/60 hover:text-white px-1.5 py-0.5 rounded border border-white/10"
          disabled={loading}
        >
          {loading ? "…" : "Refresh"}
        </button>
      </div>
      {err ? <div className="text-[11px] text-red-300 mb-2">{err}</div> : null}
      {files.length === 0 && !loading ? (
        <div className="text-[12px] text-white/40">No files yet.</div>
      ) : (
        <ul className="space-y-1 max-h-[360px] overflow-auto">
          {files.map((f) => (
            <li
              key={f.path}
              className="group flex items-center justify-between gap-2 text-[12px] rounded-md px-2 py-1 hover:bg-white/5"
            >
              <div className="min-w-0 flex-1">
                <div className="truncate text-white/85" title={f.path}>
                  {f.name}
                </div>
                <div className="text-[10px] text-white/40">{formatSize(f.size)}</div>
              </div>
              <button
                onClick={() => handleDownload(f)}
                className="opacity-0 group-hover:opacity-100 transition-opacity text-[11px] text-emerald-300 hover:text-emerald-200 px-1.5 py-0.5 rounded border border-emerald-400/30"
              >
                Download
              </button>
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}
