/**
 * LiveStudio — the split view when a run is active.
 *
 * Left:  LiveCanvas (Plan / Activity / Artifacts)
 * Right: PreviewPane (iframe with viewport switcher)
 * Bottom (terminal+success): ReceiptStrip — preview URL · credits · time
 *
 * Stacks vertically below `lg`.  No hero copy, no celebratory emojis.
 */
import { useEffect, useMemo, useState } from "react";
import { Check, Copy, ExternalLink } from "lucide-react";
import { LiveCanvas } from "@/components/deft/LiveCanvas";
import { PreviewPane } from "@/components/deft/PreviewPane";
import { previewAbsoluteUrl, type AgentEvent, type AgentTaskState } from "@/lib/agentRunApi";
import { creditsFromUsd, durationSeconds } from "@/components/deft/studio/stage";
import { cn } from "@/lib/utils";

interface LiveStudioProps {
  task: AgentTaskState;
  events: AgentEvent[];
  connectionStatus: "live" | "polling" | "closed";
  onCancel: () => void;
  taskId: string;
  /** Optional preview URL — if provided, shown in the receipt strip on terminal success. */
  previewUrl?: string | null;
  className?: string;
}

const TERMINAL_OK = new Set(["done", "completed"]);

export function LiveStudio({
  task,
  events,
  connectionStatus,
  onCancel,
  taskId,
  previewUrl,
  className,
}: LiveStudioProps) {
  const isLive = TERMINAL_OK.has(task.state);
  const credits = creditsFromUsd(task.spent_usd);
  const seconds = durationSeconds(task);
  const derivedUrl = useMemo(() => previewUrl ?? deriveLiveUrl(events, task), [previewUrl, events, task]);

  return (
    <div
      className={cn(
        "flex h-full min-h-0 flex-col gap-3 p-3",
        className,
      )}
    >
      <div
        className={cn(
          "grid min-h-0 flex-1 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-3",
          "lg:grid-cols-[minmax(360px,1fr)_minmax(0,1.35fr)] lg:grid-rows-1",
        )}
      >
        <div className="min-h-0">
          <LiveCanvas
            task={task}
            events={events}
            connectionStatus={connectionStatus}
            onCancel={onCancel}
            className="h-full"
          />
        </div>
        <div className="min-h-0">
          <PreviewPane
            taskId={taskId}
            task={task}
            events={events}
            className="h-full"
          />
        </div>
      </div>
      {isLive && derivedUrl && (
        <ReceiptStrip url={derivedUrl} credits={credits} seconds={seconds} />
      )}
    </div>
  );
}

interface ReceiptStripProps {
  url: string;
  credits: number;
  seconds: number;
}

function ReceiptStrip({ url, credits, seconds }: ReceiptStripProps) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1200);
    return () => window.clearTimeout(t);
  }, [copied]);

  const onCopy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
    } catch {
      // ignore
    }
  };

  return (
    <div
      role="status"
      aria-label="Run receipt"
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-deploy/40 bg-deploy/5 px-3 py-2 text-xs",
      )}
    >
      <span className="inline-flex items-center gap-1.5 font-medium text-foreground">
        <span className="size-1.5 rounded-full bg-deploy" aria-hidden="true" />
        Live
      </span>
      <span className="text-muted-foreground">at</span>
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        className="truncate font-mono text-foreground underline-offset-2 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
      >
        {url.replace(/^https?:\/\//, "")}
      </a>
      <button
        type="button"
        onClick={onCopy}
        aria-label={copied ? "URL copied" : "Copy URL"}
        title={copied ? "Copied" : "Copy URL"}
        className={cn(
          "inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors",
          "hover:bg-card hover:text-foreground",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
        )}
      >
        {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
      </button>
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        aria-label="Open in new tab"
        title="Open in new tab"
        className={cn(
          "inline-flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors",
          "hover:bg-card hover:text-foreground",
          "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
        )}
      >
        <ExternalLink size={12} aria-hidden="true" />
      </a>
      <span className="ml-auto flex items-center gap-3 text-muted-foreground">
        <span>
          <span className="font-mono text-foreground">{credits.toLocaleString()}</span> credits
        </span>
        <span>·</span>
        <span>
          <span className="font-mono text-foreground">{formatDuration(seconds)}</span>
        </span>
      </span>
    </div>
  );
}

/** Walk events backwards looking for a deploy_preview tool result with a url, or task.artifacts. */
function deriveLiveUrl(events: AgentEvent[], task: AgentTaskState): string | null {
  // Check task.artifacts first
  const artifacts = (task.artifacts ?? []) as Array<Record<string, unknown>>;
  for (const a of artifacts) {
    const u = (a.url as string) ?? (a.signed_url as string);
    if (u && /^https?:|^\/preview\//.test(u)) {
      return /^https?:/.test(u) ? u : previewAbsoluteUrl(u);
    }
  }
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    const p = e.payload ?? {};
    const url = (p.url as string) ?? (p.preview_url as string);
    const tool = (p.tool as string) ?? (p.tool_name as string);
    if (url && (tool === "deploy_preview" || url.startsWith("/preview/") || /^https?:/.test(url))) {
      return /^https?:/.test(url) ? url : previewAbsoluteUrl(url);
    }
  }
  return null;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}m ${s.toString().padStart(2, "0")}s`;
}
