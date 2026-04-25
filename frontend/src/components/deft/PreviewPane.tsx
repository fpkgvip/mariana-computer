/**
 * PreviewPane — the right side of /build.
 *
 * Polls the deploy_preview manifest for an active task, then streams in the
 * deployed URL inside an iframe with a "magic moment" deploy-pulse glow on
 * first ready.  Until the agent ships, we show a calm phosphor placeholder
 * with the current stage.
 *
 * Three states:
 *   • idle      — agent hasn't shipped anything yet (waiting / building)
 *   • deployed  — manifest says deployed=true; iframe is live
 *   • errored   — task failed before deploying
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ExternalLink,
  Loader2,
  RefreshCw,
  Globe,
  Smartphone,
  Monitor,
  Tablet,
  Copy,
  Check,
} from "lucide-react";
import { cn } from "@/lib/utils";
import {
  type AgentEvent,
  type AgentPreviewManifest,
  type AgentTaskState,
  getAgentPreview,
  previewAbsoluteUrl,
} from "@/lib/agentRunApi";

type Viewport = "phone" | "tablet" | "desktop";

const VIEWPORTS: Record<Viewport, { label: string; w: number; h: number; Icon: typeof Smartphone }> = {
  phone: { label: "Phone", w: 390, h: 780, Icon: Smartphone },
  tablet: { label: "Tablet", w: 820, h: 1100, Icon: Tablet },
  desktop: { label: "Desktop", w: 0, h: 0, Icon: Monitor }, // 0 = stretch
};

const TERMINAL_STATES = new Set(["done", "completed", "failed", "stopped", "cancelled", "error"]);

interface PreviewPaneProps {
  taskId: string | null;
  task: AgentTaskState | null;
  events: AgentEvent[];
  className?: string;
}

export function PreviewPane({ taskId, task, events, className }: PreviewPaneProps) {
  const [manifest, setManifest] = useState<AgentPreviewManifest | null>(null);
  const [polling, setPolling] = useState(false);
  const [viewport, setViewport] = useState<Viewport>("desktop");
  const [iframeKey, setIframeKey] = useState(0);
  const [justDeployed, setJustDeployed] = useState(false);
  const [copied, setCopied] = useState(false);
  const wasDeployedRef = useRef(false);

  // Reset on task change
  useEffect(() => {
    setManifest(null);
    setIframeKey((k) => k + 1);
    setJustDeployed(false);
    wasDeployedRef.current = false;
  }, [taskId]);

  const fetchManifest = useCallback(
    async (signal?: AbortSignal) => {
      if (!taskId) return;
      try {
        const m = await getAgentPreview(taskId, signal);
        setManifest((prev) => {
          // First transition to deployed → flash the "deployed" glow
          if (m.deployed && !wasDeployedRef.current) {
            wasDeployedRef.current = true;
            setJustDeployed(true);
            window.setTimeout(() => setJustDeployed(false), 2400);
            // Bump iframe so it loads fresh
            setIframeKey((k) => k + 1);
          }
          // If url changed (redeploy) — bump iframe
          if (prev?.url && m.url && prev.url !== m.url) setIframeKey((k) => k + 1);
          return m;
        });
      } catch {
        /* swallow — endpoint may 404 until first deploy */
      }
    },
    [taskId],
  );

  // Initial fetch + poll while task is running OR until first deploy lands
  useEffect(() => {
    if (!taskId) return;
    const ctrl = new AbortController();
    void fetchManifest(ctrl.signal);

    const isTerminal = task ? TERMINAL_STATES.has(task.state) : false;
    // Poll while task running, or terminal but never seen deployment yet (in case manifest write was racy)
    if (!isTerminal || !manifest?.deployed) {
      setPolling(true);
      const id = window.setInterval(() => {
        if (ctrl.signal.aborted) return;
        void fetchManifest(ctrl.signal);
      }, 3000);
      return () => {
        ctrl.abort();
        window.clearInterval(id);
        setPolling(false);
      };
    }
    return () => ctrl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId, task?.state, manifest?.deployed]);

  // Also re-poll when an "artifact" or "tool_finished" event for deploy_preview arrives
  useEffect(() => {
    if (!taskId || events.length === 0) return;
    const last = events[events.length - 1];
    const t = (last.payload?.tool ?? last.payload?.tool_name ?? "") as string;
    const evType = last.event_type;
    if (
      t === "deploy_preview" ||
      evType === "artifact" ||
      evType === "tool_finished" ||
      (typeof last.payload?.url === "string" && (last.payload.url as string).startsWith("/preview/"))
    ) {
      void fetchManifest();
    }
  }, [events, taskId, fetchManifest]);

  const absoluteUrl = useMemo(
    () => (manifest?.url ? previewAbsoluteUrl(manifest.url) : null),
    [manifest?.url],
  );

  const onCopyUrl = useCallback(async () => {
    if (!absoluteUrl) return;
    try {
      await navigator.clipboard.writeText(absoluteUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    } catch {
      /* no-op */
    }
  }, [absoluteUrl]);

  const taskState = task?.state ?? "idle";
  const failed = taskState === "failed" || taskState === "error";
  const isRunning = task ? !TERMINAL_STATES.has(task.state) : false;
  const stage = useMemo(() => deriveStage(events, manifest, isRunning), [events, manifest, isRunning]);

  return (
    <div
      className={cn(
        "relative flex h-full min-h-0 flex-col rounded-xl border bg-surface-1 transition-shadow",
        justDeployed
          ? "border-deploy/50 shadow-[0_0_0_4px_hsl(var(--deploy)/0.10),0_30px_80px_-30px_hsl(var(--deploy)/0.55)]"
          : "border-border/70",
        className,
      )}
    >
      {/* URL bar */}
      <div className="flex items-center gap-2 border-b border-border/70 bg-surface-1/95 px-3 py-2">
        <div className="flex items-center gap-1">
          <span className="size-2 rounded-full bg-rose-500/60" aria-hidden />
          <span className="size-2 rounded-full bg-amber-400/60" aria-hidden />
          <span
            className={cn(
              "size-2 rounded-full",
              manifest?.deployed ? "bg-deploy animate-pulse" : "bg-emerald-400/40",
            )}
            aria-hidden
          />
        </div>

        <div className="ml-2 flex flex-1 items-center gap-2 truncate rounded-md border border-border/60 bg-background/60 px-2.5 py-1 font-mono text-[11.5px] text-muted-foreground">
          <Globe size={12} className={manifest?.deployed ? "text-deploy" : "text-muted-foreground"} />
          <span className="truncate text-foreground/90">
            {absoluteUrl ?? "preview.deft.computer/" + (taskId ?? "—")}
          </span>
          {polling && !manifest?.deployed && (
            <span className="ml-auto inline-flex items-center gap-1 text-[10px] text-muted-foreground">
              <Loader2 size={10} className="animate-spin" />
              waiting
            </span>
          )}
        </div>

        <div className="ml-1 flex items-center gap-1">
          {(Object.keys(VIEWPORTS) as Viewport[]).map((v) => {
            const Icon = VIEWPORTS[v].Icon;
            return (
              <button
                key={v}
                type="button"
                onClick={() => setViewport(v)}
                aria-label={VIEWPORTS[v].label}
                aria-pressed={viewport === v}
                className={cn(
                  "inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors",
                  viewport === v
                    ? "bg-surface-3 text-foreground"
                    : "text-muted-foreground hover:bg-surface-2 hover:text-foreground",
                )}
              >
                <Icon size={13} />
              </button>
            );
          })}
          <div className="mx-1 h-5 w-px bg-border/60" aria-hidden />
          <button
            type="button"
            onClick={() => setIframeKey((k) => k + 1)}
            disabled={!manifest?.deployed}
            aria-label="Reload preview"
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground disabled:opacity-40"
          >
            <RefreshCw size={13} />
          </button>
          <button
            type="button"
            onClick={onCopyUrl}
            disabled={!absoluteUrl}
            aria-label="Copy preview URL"
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-surface-2 hover:text-foreground disabled:opacity-40"
          >
            {copied ? <Check size={13} className="text-deploy" /> : <Copy size={13} />}
          </button>
          <a
            href={absoluteUrl ?? "#"}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Open preview in new tab"
            aria-disabled={!absoluteUrl}
            onClick={(e) => {
              if (!absoluteUrl) e.preventDefault();
            }}
            className={cn(
              "inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors",
              absoluteUrl
                ? "text-muted-foreground hover:bg-surface-2 hover:text-foreground"
                : "pointer-events-none opacity-40 text-muted-foreground",
            )}
          >
            <ExternalLink size={13} />
          </a>
        </div>
      </div>

      {/* Body */}
      <div className="relative flex-1 overflow-hidden bg-[#0b0b11]">
        {failed && !manifest?.deployed ? (
          <FailedState reason={(task?.error ?? "Run ended before deploy") as string} />
        ) : manifest?.deployed && absoluteUrl ? (
          <div className="flex h-full w-full items-center justify-center overflow-auto p-3">
            <div
              className={cn(
                "relative flex items-center justify-center bg-background shadow-[0_30px_120px_-30px_rgba(0,0,0,0.8)] transition-all duration-300",
                viewport === "desktop" ? "h-full w-full rounded-md" : "rounded-[18px] border border-border/40",
              )}
              style={
                viewport === "desktop"
                  ? undefined
                  : {
                      width: VIEWPORTS[viewport].w,
                      height: Math.min(VIEWPORTS[viewport].h, 1100),
                    }
              }
            >
              <iframe
                key={iframeKey}
                src={absoluteUrl}
                title="Live preview"
                sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-downloads"
                className={cn(
                  "h-full w-full bg-white",
                  viewport === "desktop" ? "rounded-md" : "rounded-[14px]",
                )}
              />
            </div>
          </div>
        ) : (
          <WaitingState stage={stage} />
        )}

        {/* Deploy moment flash overlay */}
        {justDeployed && (
          <div
            className="pointer-events-none absolute inset-0 animate-deploy-pulse"
            style={{
              background:
                "radial-gradient(closest-side at 50% 30%, hsl(var(--deploy)/0.18), transparent 60%)",
            }}
            aria-hidden
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

function FailedState({ reason }: { reason: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-8 text-center">
      <AlertTriangle size={28} className="text-destructive" />
      <h3 className="text-base font-medium text-foreground">Run ended without a deploy</h3>
      <p className="max-w-md text-sm text-muted-foreground">{reason}</p>
    </div>
  );
}

function WaitingState({ stage }: { stage: ReturnType<typeof deriveStage> }) {
  const items: Array<{ key: string; label: string; caption: string }> = [
    { key: "plan", label: "Plan", caption: "Breaking your goal into steps" },
    { key: "write", label: "Write", caption: "Generating files in the sandbox" },
    { key: "compile", label: "Compile", caption: "Type-checking and bundling" },
    { key: "verify", label: "Verify", caption: "Running it, catching errors" },
    { key: "live", label: "Live", caption: "Pushing to a preview URL" },
  ];
  const currentIdx = items.findIndex((s) => s.key === stage.key);

  return (
    <div className="flex h-full flex-col items-center justify-center gap-8 px-8 text-center">
      <div className="relative flex h-20 w-20 items-center justify-center">
        <div
          className="absolute inset-0 rounded-full opacity-60 blur-2xl"
          style={{ background: "radial-gradient(closest-side, hsl(var(--accent)/0.45), transparent)" }}
          aria-hidden
        />
        <Loader2 size={32} className="animate-spin text-accent" />
      </div>
      <div className="space-y-1.5">
        <p className="text-[12px] font-medium tracking-[0.02em] text-accent">{stage.eyebrow}</p>
        <h3 className="text-lg font-semibold text-foreground">{stage.title}</h3>
        <p className="text-sm text-muted-foreground">{stage.subtitle}</p>
      </div>
      <ol className="flex w-full max-w-[420px] flex-col gap-2 rounded-lg border border-border/60 bg-surface-1/60 p-3 text-left">
        {items.map((s, i) => {
          const done = i < currentIdx;
          const active = i === currentIdx;
          return (
            <li key={s.key} className="flex items-center gap-3 text-[13px]">
              <span
                className={cn(
                  "inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full border font-mono text-[10px]",
                  done && "border-deploy bg-deploy/15 text-deploy",
                  active && "border-accent bg-accent/15 text-accent",
                  !done && !active && "border-border/60 text-muted-foreground",
                )}
              >
                {done ? "✓" : i + 1}
              </span>
              <span
                className={cn(
                  "flex-1",
                  active ? "text-foreground" : done ? "text-foreground/70" : "text-muted-foreground",
                )}
              >
                <span className="font-medium">{s.label}</span>
                <span className="ml-2 text-muted-foreground/80">{s.caption}</span>
              </span>
              {active && <Loader2 size={11} className="animate-spin text-accent" />}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function deriveStage(
  events: AgentEvent[],
  manifest: AgentPreviewManifest | null,
  isRunning: boolean,
): { key: "plan" | "write" | "compile" | "verify" | "live" | "done"; eyebrow: string; title: string; subtitle: string } {
  if (manifest?.deployed) {
    return { key: "done", eyebrow: "Live", title: "Your app is live.", subtitle: "Iframe is loading the preview URL." };
  }
  // Walk recent events backward to figure out where we are.
  for (let i = events.length - 1; i >= 0 && i >= events.length - 60; i--) {
    const e = events[i];
    const tool = (e.payload?.tool ?? e.payload?.tool_name ?? "") as string;
    if (tool === "deploy_preview")
      return {
        key: "live",
        eyebrow: "Live",
        title: "Deploying your preview…",
        subtitle: "Snapshotting files and publishing the URL.",
      };
    if (tool === "browser_screenshot" || e.event_type === "verify")
      return {
        key: "verify",
        eyebrow: "Verify",
        title: "Running it for real…",
        subtitle: "Opening the app in a browser to catch errors.",
      };
    if (tool === "sandbox_exec" || tool === "exec" || /build|vite|tsc|npm/.test(JSON.stringify(e.payload ?? {})))
      return {
        key: "compile",
        eyebrow: "Compile",
        title: "Compiling your project…",
        subtitle: "Type-checking, bundling, optimizing assets.",
      };
    if (tool === "fs_write" || tool === "fs_edit")
      return {
        key: "write",
        eyebrow: "Write",
        title: "Writing your code…",
        subtitle: "Generating files based on the plan.",
      };
    if (e.event_type === "plan" || tool === "plan")
      return {
        key: "plan",
        eyebrow: "Plan",
        title: "Drafting the plan…",
        subtitle: "Breaking your goal into clean, ordered steps.",
      };
  }
  if (!isRunning && events.length === 0)
    return { key: "plan", eyebrow: "Idle", title: "Ready when you are.", subtitle: "Type below and press Enter." };
  return { key: "plan", eyebrow: "Plan", title: "Planning your build…", subtitle: "This usually takes a few seconds." };
}
