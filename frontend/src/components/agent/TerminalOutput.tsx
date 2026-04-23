import { useEffect, useRef } from "react";

export interface TerminalLine {
  id: string;
  kind: "stdout" | "stderr" | "cmd" | "info" | "error";
  text: string;
  ts?: number;
}

export interface TerminalOutputProps {
  lines: TerminalLine[];
  maxLines?: number;
  autoscroll?: boolean;
  height?: string;
}

/**
 * Terminal-style log viewer with auto-scroll and color-coded lines.
 * Renders the last N lines (default 1000) to keep DOM bounded.
 */
export function TerminalOutput({
  lines,
  maxLines = 1000,
  autoscroll = true,
  height = "320px",
}: TerminalOutputProps) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const visible = lines.length > maxLines ? lines.slice(-maxLines) : lines;

  useEffect(() => {
    if (!autoscroll || !scrollRef.current) return;
    scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [visible.length, autoscroll]);

  if (visible.length === 0) {
    return (
      <div
        className="rounded-lg border border-white/10 bg-black/80 font-mono text-xs text-white/40 p-3 flex items-center justify-center"
        style={{ height }}
      >
        Terminal idle. Output will stream here when the agent runs commands.
      </div>
    );
  }

  return (
    <div
      ref={scrollRef}
      className="rounded-lg border border-white/10 bg-black/80 font-mono text-[12px] leading-5 overflow-auto p-3"
      style={{ height }}
    >
      {visible.map((line) => {
        const color =
          line.kind === "stderr" || line.kind === "error"
            ? "text-red-400"
            : line.kind === "cmd"
            ? "text-emerald-300"
            : line.kind === "info"
            ? "text-sky-300"
            : "text-white/85";
        const prefix =
          line.kind === "cmd" ? "$ " : line.kind === "stderr" ? "! " : line.kind === "info" ? "· " : "";
        return (
          <div key={line.id} className={`whitespace-pre-wrap break-words ${color}`}>
            {prefix}
            {line.text}
          </div>
        );
      })}
    </div>
  );
}
