/**
 * F1 — Prompt Bar
 *
 * The primary entry point for kicking off an agent run.
 *
 * Behaviors:
 *  - Single textarea, auto-grow to ~4 lines
 *  - Enter sends, Shift+Enter newline
 *  - Cmd/Ctrl+K opens recent prompts dropdown
 *  - Slash commands: /research /build /code /analyze /skill — show hover preview
 *  - Persists last 10 prompts to localStorage under STORAGE.recentPrompts
 *  - Fully keyboard navigable; ARIA labels on every interactive element
 */

import {
  forwardRef,
  KeyboardEvent,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { ArrowUp, Sparkles, Clock, Loader2 } from "lucide-react";
import { STORAGE } from "@/lib/brand";
import { cn } from "@/lib/utils";

const MAX_RECENT = 10;
const MAX_PROMPT_CHARS = 8_000;

const SLASH_COMMANDS: Array<{
  trigger: string;
  label: string;
  hint: string;
}> = [
  { trigger: "/research", label: "Research", hint: "Investigate, compare, summarize." },
  { trigger: "/build", label: "Build", hint: "Build an app or feature end to end." },
  { trigger: "/code", label: "Code", hint: "Write, refactor, or debug code." },
  { trigger: "/analyze", label: "Analyze", hint: "Inspect data, logs, or repos." },
  { trigger: "/skill", label: "Skill", hint: "Run a saved skill by name." },
];

export interface PromptBarHandle {
  focus: () => void;
  clear: () => void;
}

interface PromptBarProps {
  onSubmit: (prompt: string) => void | Promise<void>;
  /** Disable input + submit button (e.g. while a run is starting). */
  disabled?: boolean;
  /** Show inline spinner on submit button. */
  busy?: boolean;
  placeholder?: string;
  /** Initial textarea value (uncontrolled). */
  initialValue?: string;
  className?: string;
  /** Optional: notify parent of every keystroke (used by Pre-flight quote). */
  onChange?: (value: string) => void;
}

function loadRecent(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE.recentPrompts);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    if (!Array.isArray(arr)) return [];
    return arr.filter((s) => typeof s === "string").slice(0, MAX_RECENT);
  } catch {
    return [];
  }
}

function saveRecent(prompt: string) {
  try {
    const trimmed = prompt.trim();
    if (!trimmed) return;
    const cur = loadRecent().filter((p) => p !== trimmed);
    const next = [trimmed, ...cur].slice(0, MAX_RECENT);
    localStorage.setItem(STORAGE.recentPrompts, JSON.stringify(next));
  } catch {
    // ignore quota / disabled storage
  }
}

export const PromptBar = forwardRef<PromptBarHandle, PromptBarProps>(function PromptBar(
  {
    onSubmit,
    disabled = false,
    busy = false,
    placeholder = "What should Deft build?",
    initialValue = "",
    className,
    onChange,
  },
  ref,
) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const [value, setValue] = useState<string>(initialValue);
  const [recentOpen, setRecentOpen] = useState(false);
  const [slashOpen, setSlashOpen] = useState(false);
  const [recent, setRecent] = useState<string[]>(() => loadRecent());

  useImperativeHandle(ref, () => ({
    focus: () => textareaRef.current?.focus(),
    clear: () => setValue(""),
  }));

  // Auto-grow textarea
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const max = 4 * 24 + 24; // ~4 lines
    ta.style.height = `${Math.min(ta.scrollHeight, max)}px`;
  }, [value]);

  // Watch for slash command at start of buffer
  useEffect(() => {
    setSlashOpen(value.startsWith("/") && value.length <= 16 && !value.includes(" "));
  }, [value]);

  const filteredCmds = useMemo(() => {
    if (!slashOpen) return [] as typeof SLASH_COMMANDS;
    return SLASH_COMMANDS.filter((c) => c.trigger.startsWith(value));
  }, [slashOpen, value]);

  const handleChange = useCallback(
    (next: string) => {
      const truncated = next.length > MAX_PROMPT_CHARS ? next.slice(0, MAX_PROMPT_CHARS) : next;
      setValue(truncated);
      onChange?.(truncated);
    },
    [onChange],
  );

  const submit = useCallback(async () => {
    const trimmed = value.trim();
    if (!trimmed || disabled || busy) return;
    saveRecent(trimmed);
    setRecent(loadRecent());
    await onSubmit(trimmed);
  }, [value, disabled, busy, onSubmit]);

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Cmd/Ctrl + K → recent prompts
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setRecentOpen((o) => !o);
        return;
      }
      // Enter to submit; Shift+Enter for newline
      if (e.key === "Enter" && !e.shiftKey) {
        // If slash command picker is open and we have at least one match, expand
        if (slashOpen && filteredCmds.length === 1) {
          e.preventDefault();
          handleChange(filteredCmds[0].trigger + " ");
          return;
        }
        e.preventDefault();
        void submit();
        return;
      }
      if (e.key === "Escape") {
        if (recentOpen) {
          e.preventDefault();
          setRecentOpen(false);
        }
      }
    },
    [recentOpen, slashOpen, filteredCmds, handleChange, submit],
  );

  const pickRecent = (prompt: string) => {
    handleChange(prompt);
    setRecentOpen(false);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const charCount = value.length;
  const canSubmit = !disabled && !busy && value.trim().length > 0;

  return (
    <div className={cn("relative w-full", className)}>
      {/* Recent prompts dropdown */}
      {recentOpen && recent.length > 0 && (
        <div
          role="listbox"
          aria-label="Recent prompts"
          className="absolute bottom-full left-0 right-0 z-10 mb-2 max-h-72 overflow-auto rounded-lg border border-border bg-popover shadow-lg"
        >
          <div className="flex items-center gap-2 border-b border-border px-3 py-2 text-xs text-muted-foreground">
            <Clock size={12} aria-hidden />
            Recent prompts
          </div>
          <ul className="py-1">
            {recent.map((p, i) => (
              <li key={`${p}-${i}`}>
                <button
                  type="button"
                  className="block w-full truncate px-3 py-2 text-left text-sm text-foreground transition-colors hover:bg-secondary focus:bg-secondary focus:outline-none"
                  onClick={() => pickRecent(p)}
                >
                  {p}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Slash command picker */}
      {slashOpen && filteredCmds.length > 0 && (
        <div
          role="listbox"
          aria-label="Slash commands"
          className="absolute bottom-full left-0 right-0 z-10 mb-2 overflow-hidden rounded-lg border border-border bg-popover shadow-lg"
        >
          <ul className="py-1">
            {filteredCmds.map((c) => (
              <li key={c.trigger}>
                <button
                  type="button"
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm text-foreground transition-colors hover:bg-secondary focus:bg-secondary focus:outline-none"
                  onClick={() => {
                    handleChange(`${c.trigger} `);
                    setSlashOpen(false);
                    requestAnimationFrame(() => textareaRef.current?.focus());
                  }}
                >
                  <span className="flex items-center gap-2">
                    <span className="font-mono text-xs text-accent">{c.trigger}</span>
                    <span>{c.label}</span>
                  </span>
                  <span className="truncate text-xs text-muted-foreground">{c.hint}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div
        className={cn(
          "flex items-end gap-2 rounded-xl border border-border bg-card px-3 py-2 transition-colors",
          "focus-within:border-accent",
          disabled && "opacity-60",
        )}
      >
        <Sparkles
          size={16}
          aria-hidden
          className="mb-2 shrink-0 text-muted-foreground"
        />
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled}
          placeholder={placeholder}
          aria-label="Prompt"
          aria-describedby="prompt-bar-hints"
          rows={1}
          className={cn(
            "min-h-[24px] max-h-[120px] flex-1 resize-none border-0 bg-transparent",
            "py-2 text-sm leading-6 text-foreground outline-none",
            "placeholder:text-[hsl(var(--fg-3))]",
          )}
        />
        <button
          type="button"
          onClick={submit}
          disabled={!canSubmit}
          aria-label="Submit prompt"
          className={cn(
            "mb-1 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
            "transition-all duration-150",
            canSubmit
              ? "bg-accent text-accent-foreground hover:opacity-90 active:scale-95"
              : "cursor-not-allowed bg-secondary text-muted-foreground",
          )}
        >
          {busy ? <Loader2 size={16} className="animate-spin" /> : <ArrowUp size={16} />}
        </button>
      </div>

      <div
        id="prompt-bar-hints"
        className="mt-1.5 flex items-center justify-between px-1 text-[11px] text-muted-foreground"
      >
        <span>
          <kbd className="rounded border border-border bg-secondary px-1 py-px font-mono">
            Enter
          </kbd>{" "}
          send ·{" "}
          <kbd className="rounded border border-border bg-secondary px-1 py-px font-mono">
            ⌘K
          </kbd>{" "}
          recent ·{" "}
          <kbd className="rounded border border-border bg-secondary px-1 py-px font-mono">
            /
          </kbd>{" "}
          commands
        </span>
        <span aria-live="polite" aria-atomic="true">
          {charCount > 0 && `${charCount.toLocaleString()} / ${MAX_PROMPT_CHARS.toLocaleString()}`}
        </span>
      </div>
    </div>
  );
});
