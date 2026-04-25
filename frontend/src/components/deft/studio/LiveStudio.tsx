/**
 * LiveStudio — the split view when a run is active.
 *
 * Left:  LiveCanvas (Plan / Activity / Artifacts)
 * Right: PreviewPane (iframe with viewport switcher)
 *
 * Stacks vertically below `lg`.  No hero copy, no celebratory emojis.
 */
import { LiveCanvas } from "@/components/deft/LiveCanvas";
import { PreviewPane } from "@/components/deft/PreviewPane";
import type { AgentEvent, AgentTaskState } from "@/lib/agentRunApi";
import { cn } from "@/lib/utils";

interface LiveStudioProps {
  task: AgentTaskState;
  events: AgentEvent[];
  connectionStatus: "live" | "polling" | "closed";
  onCancel: () => void;
  taskId: string;
  className?: string;
}

export function LiveStudio({
  task,
  events,
  connectionStatus,
  onCancel,
  taskId,
  className,
}: LiveStudioProps) {
  return (
    <div
      className={cn(
        "grid h-full min-h-0 grid-rows-[minmax(0,1fr)_minmax(0,1fr)] gap-3 p-3",
        "lg:grid-cols-[minmax(360px,1fr)_minmax(0,1.35fr)] lg:grid-rows-1",
        className,
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
  );
}
