import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { AgentTaskView } from "@/components/agent/AgentTaskView";
import {
  ArrowLeft,
  CheckCircle2,
  AlertCircle,
  Clock,
  Play,
  Download,
  FileText,
  FileImage,
  FileVideo,
  FileCode2,
  FileSpreadsheet,
  File as FileIcon,
  Check,
  X,
  Loader2,
  ShieldAlert,
} from "lucide-react";
import { toast } from "sonner";

const API_URL = import.meta.env.VITE_API_URL ?? "";

interface TaskDetail {
  id: string;
  user_id: string;
  goal: string;
  state: string;
  selected_model: string;
  budget_usd: number;
  spent_usd: number;
  replan_count: number;
  total_failures: number;
  error: string | null;
  final_answer: string | null;
  artifacts: Artifact[];
  created_at: string;
  updated_at: string;
}

interface Artifact {
  name: string;
  workspace_path: string;
  size: number;
  sha256: string;
  produced_by_step?: string | null;
}

interface PendingApproval {
  approval_id: string;
  event_id: number;
  requested_at: string;
  summary: string;
  tool: string;
  params: Record<string, unknown>;
  tier: string;
}

const TERMINAL_STATES = new Set(["done", "failed", "halted"]);

function stateBadge(state: string) {
  const s = state.toLowerCase();
  if (s === "done") {
    return { icon: <CheckCircle2 size={13} />, label: "Done", cn: "bg-emerald-500/10 text-emerald-400 ring-emerald-500/20" };
  }
  if (s === "failed" || s === "halted") {
    return { icon: <AlertCircle size={13} />, label: s === "halted" ? "Halted" : "Failed", cn: "bg-red-500/10 text-red-400 ring-red-500/20" };
  }
  if (s === "plan" || s === "queued") {
    return { icon: <Clock size={13} />, label: "Queued", cn: "bg-zinc-500/10 text-zinc-400 ring-zinc-500/20" };
  }
  return { icon: <Play size={13} />, label: s.charAt(0).toUpperCase() + s.slice(1), cn: "bg-blue-500/10 text-blue-400 ring-blue-500/20" };
}

function iconForFile(name: string) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "svg"].includes(ext)) return <FileImage size={15} />;
  if (["mp4", "mov", "webm", "gif"].includes(ext)) return <FileVideo size={15} />;
  if (["md", "txt", "pdf", "docx"].includes(ext)) return <FileText size={15} />;
  if (["xlsx", "csv"].includes(ext)) return <FileSpreadsheet size={15} />;
  if (["py", "ts", "tsx", "js", "jsx", "rs", "sh", "json"].includes(ext)) return <FileCode2 size={15} />;
  return <FileIcon size={15} />;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export default function TaskDetail() {
  const { taskId = "" } = useParams();
  const { user } = useAuth();
  const navigate = useNavigate();
  const [task, setTask] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [approvals, setApprovals] = useState<PendingApproval[]>([]);
  const [approvalLoading, setApprovalLoading] = useState<string | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const getToken = useCallback(async (): Promise<string | null> => {
    const { data: { session } } = await supabase.auth.getSession();
    return session?.access_token ?? null;
  }, []);

  // Grace period for auth refresh
  useEffect(() => {
    if (!user) {
      const t = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(t);
    }
  }, [user, navigate]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Load task detail
  const loadTask = useCallback(async () => {
    try {
      const token = await getToken();
      if (!token) return;
      const res = await fetch(`${API_URL}/api/agent/${encodeURIComponent(taskId)}`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${await res.text().catch(() => res.statusText)}`);
      }
      const data: TaskDetail = await res.json();
      if (mountedRef.current) setTask(data);
    } catch (e) {
      if (mountedRef.current) setError(e instanceof Error ? e.message : String(e));
    }
  }, [taskId, getToken]);

  const loadApprovals = useCallback(async () => {
    try {
      const token = await getToken();
      if (!token) return;
      const res = await fetch(`${API_URL}/api/agent/${encodeURIComponent(taskId)}/approvals`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      if (!res.ok) return;
      const data: { approvals: PendingApproval[] } = await res.json();
      if (mountedRef.current) setApprovals(Array.isArray(data.approvals) ? data.approvals : []);
    } catch {
      // silent — approvals are secondary
    }
  }, [taskId, getToken]);

  useEffect(() => {
    if (!user || !taskId) return;
    loadTask();
    loadApprovals();
    // Poll while task is not terminal
    const iv = setInterval(() => {
      loadTask();
      loadApprovals();
    }, 5_000);
    return () => clearInterval(iv);
  }, [user, taskId, loadTask, loadApprovals]);

  const isTerminal = task && TERMINAL_STATES.has(task.state.toLowerCase());

  const handleApproval = async (approvalId: string, decision: "approve" | "deny") => {
    setApprovalLoading(approvalId);
    try {
      const token = await getToken();
      if (!token) throw new Error("not authenticated");
      const res = await fetch(
        `${API_URL}/api/agent/${encodeURIComponent(taskId)}/approvals/${encodeURIComponent(approvalId)}/decide`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ decision }),
        },
      );
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${await res.text().catch(() => res.statusText)}`);
      }
      setApprovals((prev) => prev.filter((a) => a.approval_id !== approvalId));
      toast.success(decision === "approve" ? "Approved" : "Denied", {
        description: decision === "approve" ? "Agent will continue." : "Step will be skipped.",
      });
    } catch (e) {
      toast.error("Decision failed", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setApprovalLoading(null);
    }
  };

  const downloadArtifact = async (art: Artifact) => {
    if (!user) return;
    setDownloading(art.workspace_path);
    try {
      const token = await getToken();
      if (!token) throw new Error("not authenticated");
      const url = `${API_URL}/api/workspace/${encodeURIComponent(user.id)}/file?path=${encodeURIComponent(art.workspace_path)}&binary=true`;
      const res = await fetch(url, { headers: { Authorization: `Bearer ${token}` } });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}: ${await res.text().catch(() => res.statusText)}`);
      }
      // Backend streams binary files as application/octet-stream, and returns
      // JSON { content / content_b64 } for text-mode reads.  Detect both.
      const ct = (res.headers.get("content-type") || "").toLowerCase();
      let blob: Blob;
      if (ct.includes("application/json")) {
        const data = await res.json();
        const b64 = (data.content_b64 ?? data.content_base64) as string | undefined;
        const text = data.content as string | undefined;
        blob = b64
          ? await fetch(`data:application/octet-stream;base64,${b64}`).then((r) => r.blob())
          : new Blob([text ?? ""], { type: "text/plain" });
      } else {
        blob = await res.blob();
      }
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = blobUrl;
      a.download = art.name || art.workspace_path.split("/").pop() || "artifact";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(blobUrl), 5_000);
    } catch (e) {
      toast.error("Download failed", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setDownloading(null);
    }
  };

  const badge = useMemo(() => (task ? stateBadge(task.state) : null), [task]);

  if (!user) {
    return (
      <div className="min-h-screen bg-background">
        <Navbar />
        <section className="px-6 pt-32 pb-16">
          <div className="mx-auto max-w-5xl">
            <div className="h-8 w-48 animate-pulse rounded bg-muted" />
            <div className="mt-8 h-96 animate-pulse rounded-lg border border-border bg-card/50" />
          </div>
        </section>
        <Footer />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-5xl">
          <Link
            to="/tasks"
            className="inline-flex items-center gap-2 text-xs font-medium text-muted-foreground hover:text-foreground"
          >
            <ArrowLeft size={13} />
            All tasks
          </Link>

          {error && !task && (
            <div className="mt-8 rounded-lg border border-red-500/30 bg-red-500/5 p-4 text-sm text-red-400">
              Could not load task. {error}
            </div>
          )}

          {task && (
            <>
              <header className="mt-4">
                <div className="flex items-start justify-between gap-4 flex-wrap">
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      {badge && (
                        <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider ring-1 ring-inset ${badge.cn}`}>
                          {badge.icon}
                          {badge.label}
                        </span>
                      )}
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {task.id.slice(0, 8)}
                      </span>
                    </div>
                    <h1 className="mt-3 font-serif text-xl font-semibold text-foreground sm:text-2xl">
                      {task.goal}
                    </h1>
                    <p className="mt-2 font-mono text-xs text-muted-foreground">
                      {task.selected_model} · ${task.spent_usd.toFixed(3)} of ${task.budget_usd.toFixed(2)}
                      {task.replan_count > 0 && ` · ${task.replan_count} replan${task.replan_count === 1 ? "" : "s"}`}
                    </p>
                  </div>
                </div>
              </header>

              {/* Approval queue — shown prominently when any pending */}
              {approvals.length > 0 && (
                <section className="mt-6 rounded-lg border border-amber-500/30 bg-amber-500/5 p-5">
                  <div className="flex items-center gap-2">
                    <ShieldAlert size={15} className="text-amber-400" />
                    <h2 className="text-sm font-semibold text-foreground">
                      {approvals.length} action{approvals.length === 1 ? "" : "s"} need{approvals.length === 1 ? "s" : ""} your approval
                    </h2>
                  </div>
                  <ul className="mt-4 space-y-3">
                    {approvals.map((a) => (
                      <li key={a.approval_id} className="rounded-md border border-amber-500/20 bg-background/60 p-4">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0 flex-1">
                            <p className="text-sm font-medium text-foreground">
                              {a.summary || `${a.tool} (tier ${a.tier || "B"})`}
                            </p>
                            {a.tool && (
                              <p className="mt-1 font-mono text-[11px] text-muted-foreground">
                                {a.tool}
                              </p>
                            )}
                            {Object.keys(a.params || {}).length > 0 && (
                              <pre className="mt-2 max-h-40 overflow-auto rounded bg-muted/50 p-2 font-mono text-[10px] text-muted-foreground">
                                {JSON.stringify(a.params, null, 2)}
                              </pre>
                            )}
                          </div>
                          <div className="flex shrink-0 gap-2">
                            <button
                              onClick={() => handleApproval(a.approval_id, "deny")}
                              disabled={approvalLoading === a.approval_id}
                              className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-60"
                            >
                              {approvalLoading === a.approval_id ? (
                                <Loader2 size={12} className="animate-spin" />
                              ) : (
                                <X size={12} />
                              )}
                              Deny
                            </button>
                            <button
                              onClick={() => handleApproval(a.approval_id, "approve")}
                              disabled={approvalLoading === a.approval_id}
                              className="inline-flex items-center gap-1 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-60"
                            >
                              {approvalLoading === a.approval_id ? (
                                <Loader2 size={12} className="animate-spin" />
                              ) : (
                                <Check size={12} />
                              )}
                              Approve
                            </button>
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {/* Live stream (AgentTaskView) — only while running */}
              {!isTerminal && (
                <AgentTaskView
                  taskId={task.id}
                  userId={user.id}
                  apiUrl={API_URL}
                  getToken={getToken}
                  goal={task.goal}
                  onTerminal={() => loadTask()}
                />
              )}

              {/* Final answer */}
              {task.final_answer && (
                <section className="mt-6 rounded-lg border border-border bg-card p-5">
                  <h2 className="text-sm font-semibold text-foreground">Final answer</h2>
                  <div className="mt-3 whitespace-pre-wrap break-words text-sm leading-[1.7] text-foreground/90">
                    {task.final_answer}
                  </div>
                </section>
              )}

              {/* Error */}
              {task.error && (
                <section className="mt-6 rounded-lg border border-red-500/30 bg-red-500/5 p-5">
                  <h2 className="text-sm font-semibold text-red-400">Error</h2>
                  <p className="mt-2 font-mono text-xs text-red-400/90">{task.error}</p>
                </section>
              )}

              {/* Artifacts */}
              <section className="mt-6">
                <h2 className="text-sm font-semibold text-foreground">
                  Artifacts
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {task.artifacts.length} file{task.artifacts.length === 1 ? "" : "s"}
                  </span>
                </h2>
                {task.artifacts.length === 0 ? (
                  <p className="mt-3 text-sm text-muted-foreground">
                    {isTerminal ? "This task did not produce any artifacts." : "No artifacts yet — they'll appear here as the agent writes files."}
                  </p>
                ) : (
                  <ul className="mt-3 divide-y divide-border rounded-lg border border-border bg-card">
                    {task.artifacts.map((art) => (
                      <li key={art.workspace_path} className="flex items-center gap-3 px-4 py-3">
                        <div className="shrink-0 text-muted-foreground">
                          {iconForFile(art.name)}
                        </div>
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium text-foreground">
                            {art.name}
                          </p>
                          <p className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                            {formatSize(art.size)} · {art.sha256.slice(0, 12)}
                          </p>
                        </div>
                        <button
                          onClick={() => downloadArtifact(art)}
                          disabled={downloading === art.workspace_path}
                          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-secondary hover:text-foreground disabled:opacity-60"
                          aria-label={`Download ${art.name}`}
                        >
                          {downloading === art.workspace_path ? (
                            <Loader2 size={12} className="animate-spin" />
                          ) : (
                            <Download size={12} />
                          )}
                          Download
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            </>
          )}
        </div>
      </section>

      <Footer />
    </div>
  );
}
