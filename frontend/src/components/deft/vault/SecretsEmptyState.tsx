/**
 * SecretsEmptyState — calm empty state shown when the vault is unlocked
 * but has no secrets yet.  Walks the user through the three-step pattern.
 */
import { Button } from "@/components/ui/button";
import { KeyRound, Plus } from "lucide-react";

export interface SecretsEmptyStateProps {
  onAdd: () => void;
}

export function SecretsEmptyState({ onAdd }: SecretsEmptyStateProps) {
  return (
    <div className="flex flex-col items-center gap-5 rounded-xl border border-dashed border-border/70 bg-surface-1/30 px-6 py-12 text-center">
      <div className="rounded-full border border-border/70 bg-surface-1/60 p-3">
        <KeyRound size={20} className="text-foreground/80" aria-hidden />
      </div>
      <div className="space-y-1">
        <p className="text-sm font-medium text-foreground">No secrets yet</p>
        <p className="mx-auto max-w-md text-[12.5px] leading-relaxed text-muted-foreground">
          Add an API key, then reference it in any prompt as{" "}
          <code className="rounded bg-muted/70 px-1 font-mono text-[11.5px] text-foreground">
            $KEY_NAME
          </code>
          . The agent only ever sees the sentinel.
        </p>
      </div>

      <ol className="grid w-full max-w-lg gap-2 text-left text-[12px] text-muted-foreground sm:grid-cols-3">
        <Step n={1} label="Add" body="Paste your API key. We encrypt it locally." />
        <Step n={2} label="Reference" body="Type $KEY_NAME in a prompt." />
        <Step n={3} label="Run" body="Sandbox sees the value. Output stays redacted." />
      </ol>

      <Button onClick={onAdd} size="sm">
        <Plus size={14} className="mr-1.5" aria-hidden />
        Add your first secret
      </Button>
    </div>
  );
}

function Step({ n, label, body }: { n: number; label: string; body: string }) {
  return (
    <li className="rounded-lg border border-border/60 bg-surface-1/40 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-surface-2/70 text-[10.5px] font-medium text-foreground">
          {n}
        </span>
        <span className="text-[12.5px] font-medium text-foreground">{label}</span>
      </div>
      <p className="mt-1 text-[11.5px] leading-relaxed">{body}</p>
    </li>
  );
}
