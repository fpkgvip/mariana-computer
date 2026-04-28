/**
 * F1 — Prompt Bar
 *
 * The primary entry point for kicking off an agent run.
 *
 * Behaviors:
 *  - Single textarea, auto-grow to ~4 lines
 *  - Enter sends, Shift+Enter newline
 *  - Cmd/Ctrl+K opens recent prompts dropdown
 *  - Slash commands: /code /fix /extend /research — ↑↓/Tab navigation, hover preview
 *  - Attachments: paste a URL or attach a file → chip with remove
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
import { ArrowUp, Sparkles, Clock, Loader2, Paperclip, X, Link as LinkIcon } from "lucide-react";
import { STORAGE } from "@/lib/brand";
import { cn } from "@/lib/utils";

const MAX_RECENT = 10;
const MAX_PROMPT_CHARS = 8_000;
const MAX_ATTACHMENTS = 4;
const URL_REGEX = /^(https?:\/\/|www\.)[\w./?=&%#:+\-@~]+$/i;

type SlashCmd = {
  trigger: string;
  label: string;
  hint: string;
  preview: string;
};

const SLASH_COMMANDS: SlashCmd[] = [
  {
    trigger: "/code",
    label: "Code",
    hint: "Write a new feature or component.",
    preview: "Plan the change, write the code, run it in a real browser, and verify before deploy.",
  },
  {
    trigger: "/fix",
    label: "Fix",
    hint: "Diagnose and repair a broken behavior.",
    preview: "Reproduce the bug in a sandbox, narrow the cause, patch, and re-run the failing path.",
  },
  {
    trigger: "/extend",
    label: "Extend",
    hint: "Add to an existing project.",
    preview: "Read the current code, plan the smallest viable addition, write it, and verify the new path works.",
  },
  {
    trigger: "/research",
    label: "Research",
    hint: "Investigate, compare, summarize.",
    preview: "Search, read primary sources, and produce a brief with citations. No code runs.",
  },
];

type Attachment =
  | { kind: "url"; value: string }
  | { kind: "file"; name: string; size: number };

export interface PromptBarHandle {
  focus: () => void;
  clear: () => void;
}

interface PromptBarProps {
  onSubmit: (prompt: string, attachments?: Attachment[]) => void | Promise<void>;
  /** Disable input + submit button (e.g. while a run is starting). */
  disabled?: boolean;
  /** Calm copy shown under the bar when disabled (e.g. "A run is active — cancel to start a new one."). */
  disabledHint?: string;
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

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export const PromptBar = forwardRef<PromptBarHandle, PromptBarProps>(function PromptBar(
  {
    onSubmit,
    disabled = false,
    disabledHint,
    busy = false,
    placeholder = "What should Deft do?",
    initialValue = "",
    className,
    onChange,
  },
  ref,
) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [value, setValue] = useState<string>(initialValue);
  const [recentOpen, setRecentOpen] = useState(false);
  const [slashOpen, setSlashOpen] = useState(false);
  const [slashIndex, setSlashIndex] = useState(0);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [recent, setRecent] = useState<string[]>(() => loadRecent());

  useImperativeHandle(ref, () => ({
    focus: () => textareaRef.current?.focus(),
    clear: () => {
      setValue("");
      setAttachments([]);
    },
  }));

  // Auto-grow textarea (~4 lines)
  useEffect(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = "auto";
    const max = 4 * 24 + 24;
    ta.style.height = `${Math.min(ta.scrollHeight, max)}px`;
  }, [value]);

  // Open slash picker when buffer begins with "/"
  useEffect(() => {
    setSlashOpen(value.startsWith("/") && value.length <= 16 && !value.includes(" "));
    setSlashIndex(0);
  }, [value]);

  const filteredCmds = useMemo(() => {
    if (!slashOpen) return [] as SlashCmd[];
    const matches = SLASH_COMMANDS.filter((c) => c.trigger.startsWith(value.toLowerCase()));
    return matches.length > 0 ? matches : SLASH_COMMANDS;
  }, [slashOpen, value]);

  const handleChange = useCallback(
    (next: string) => {
      const truncated = next.length > MAX_PROMPT_CHARS ? next.slice(0, MAX_PROMPT_CHARS) : next;
      setValue(truncated);
      onChange?.(truncated);
    },
    [onChange],
  );

  const addAttachment = useCallback((att: Attachment) => {
    setAttachments((prev) => {
      if (prev.length >= MAX_ATTACHMENTS) return prev;
      // Dedupe by stable key
      const key = att.kind === "url" ? att.value : att.name;
      if (prev.some((p) => (p.kind === "url" ? p.value : p.name) === key)) return prev;
      return [...prev, att];
    });
  }, []);

  const removeAttachment = useCallback((idx: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  const submit = useCallback(async () => {
    const trimmed = value.trim();
    if (!trimmed || disabled || busy) return;
    saveRecent(trimmed);
    setRecent(loadRecent());
    await onSubmit(trimmed, attachments.length > 0 ? attachments : undefined);
  }, [value, disabled, busy, onSubmit, attachments]);

  const pickSlash = useCallback(
    (cmd: SlashCmd) => {
      handleChange(`${cmd.trigger} `);
      setSlashOpen(false);
      requestAnimationFrame(() => textareaRef.current?.focus());
    },
    [handleChange],
  );

  const onKeyDown = useCallback(
    (e: KeyboardEvent<HTMLTextAreaElement>) => {
      // Cmd/Ctrl + K → recent prompts
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setRecentOpen((o) => !o);
        return;
      }

      // Slash picker navigation
      if (slashOpen && filteredCmds.length > 0) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setSlashIndex((i) => (i + 1) % filteredCmds.length);
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setSlashIndex((i) => (i - 1 + filteredCmds.length) % filteredCmds.length);
          return;
        }
        if (e.key === "Tab") {
          e.preventDefault();
          pickSlash(filteredCmds[slashIndex] ?? filteredCmds[0]);
          return;
        }
      }

      // Enter to submit; Shift+Enter for newline
      if (e.key === "Enter" && !e.shiftKey) {
        // Slash picker open with active selection → expand
        if (slashOpen && filteredCmds.length > 0) {
          e.preventDefault();
          pickSlash(filteredCmds[slashIndex] ?? filteredCmds[0]);
          return;
        }
        e.preventDefault();
        void submit();
        return;
      }

      // Detect URL paste-as-attachment: when entire buffer is a single URL
      if (e.key === "Enter" && e.shiftKey) return;

      if (e.key === "Escape") {
        if (recentOpen) {
          e.preventDefault();
          setRecentOpen(false);
        } else if (slashOpen) {
          e.preventDefault();
          setSlashOpen(false);
        }
      }
    },
    [recentOpen, slashOpen, filteredCmds, slashIndex, pickSlash, submit],
  );

  const onPaste = useCallback(
    (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
      const pasted = e.clipboardData.getData("text").trim();
      if (pasted && URL_REGEX.test(pasted) && value.trim().length === 0) {
        e.preventDefault();
        addAttachment({ kind: "url", value: pasted });
      }
    },
    [value, addAttachment],
  );

  const onFiles = useCallback(
    (files: FileList | null) => {
      if (!files) return;
      for (const f of Array.from(files).slice(0, MAX_ATTACHMENTS)) {
        addAttachment({ kind: "file", name: f.name, size: f.size });
      }
    },
    [addAttachment],
  );

  const pickRecent = (prompt: string) => {
    handleChange(prompt);
    setRecentOpen(false);
    requestAnimationFrame(() => textareaRef.current?.focus());
  };

  const charCount = value.length;
  const canSubmit = !disabled && !busy && value.trim().length > 0;
  const activeCmd = slashOpen && filteredCmds.length > 0 ? filteredCmds[slashIndex] : null;

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
            <Clock size={12} aria-hidden="true" />
            Recent prompts
          </div>
          <ul className="py-1">
            {recent.map((p, i) => (
              <li key={`${p}-${i}`}>
                <button
                  type="button"
                  className="block w-full truncate px-3 py-2 text-left text-sm text-foreground transition-colors hover:bg-secondary focus:bg-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
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
            {filteredCmds.map((c, i) => {
              const isActive = i === slashIndex;
              return (
                <li key={c.trigger}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={isActive}
                    onMouseEnter={() => setSlashIndex(i)}
                    onClick={() => pickSlash(c)}
                    className={cn(
                      "flex w-full items-center justify-between gap-3 px-3 py-2 text-left text-sm transition-colors focus:outline-none",
                      isActive ? "bg-secondary text-foreground" : "text-foreground hover:bg-secondary/60",
                    )}
                  >
                    <span className="flex items-center gap-2">
                      <span className="font-mono text-xs text-accent-strong">{c.trigger}</span>
                      <span>{c.label}</span>
                    </span>
                    <span className="truncate text-xs text-muted-foreground">{c.hint}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          {activeCmd && (
            <div className="border-t border-border bg-card px-3 py-2 text-xs text-muted-foreground">
              {activeCmd.preview}
            </div>
          )}
        </div>
      )}

      {/* Attachment chips */}
      {attachments.length > 0 && (
        <ul className="mb-2 flex flex-wrap gap-1.5" aria-label="Attachments">
          {attachments.map((a, i) => (
            <li key={i}>
              <span className="inline-flex max-w-[260px] items-center gap-1.5 rounded-md border border-border bg-secondary/60 px-2 py-1 text-xs text-foreground">
                {a.kind === "url" ? (
                  <LinkIcon size={11} aria-hidden="true" className="text-muted-foreground" />
                ) : (
                  <Paperclip size={11} aria-hidden="true" className="text-muted-foreground" />
                )}
                <span className="truncate">
                  {a.kind === "url" ? a.value : `${a.name} · ${formatBytes(a.size)}`}
                </span>
                <button
                  type="button"
                  onClick={() => removeAttachment(i)}
                  aria-label={`Remove ${a.kind === "url" ? a.value : a.name}`}
                  className="ml-0.5 rounded p-0.5 text-muted-foreground transition-colors hover:bg-card hover:text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
                >
                  <X size={11} aria-hidden="true" />
                </button>
              </span>
            </li>
          ))}
        </ul>
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
          aria-hidden="true"
          className="mb-2 shrink-0 text-muted-foreground"
        />
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => handleChange(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
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

        {/* Attach */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          onChange={(e) => onFiles(e.target.files)}
          className="hidden"
          aria-hidden="true"
          tabIndex={-1}
        />
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled || attachments.length >= MAX_ATTACHMENTS}
          aria-label="Attach file"
          title="Attach file"
          className={cn(
            "mb-1 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
            "text-muted-foreground transition-colors",
            "hover:bg-secondary hover:text-foreground",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
            "disabled:cursor-not-allowed disabled:opacity-40",
          )}
        >
          <Paperclip size={16} aria-hidden="true" />
        </button>

        {/* Submit */}
        <button
          type="button"
          onClick={submit}
          disabled={!canSubmit}
          aria-label="Submit prompt"
          className={cn(
            "mb-1 inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg",
            "transition-all duration-150",
            "focus:outline-none focus-visible:ring-2 focus-visible:ring-accent",
            canSubmit
              ? "bg-accent text-accent-foreground hover:opacity-90 active:scale-95"
              : "cursor-not-allowed bg-secondary text-muted-foreground",
          )}
        >
          {busy ? (
            <Loader2 size={16} aria-hidden="true" className="animate-spin" />
          ) : (
            <ArrowUp size={16} aria-hidden="true" />
          )}
        </button>
      </div>

      <div
        id="prompt-bar-hints"
        className="mt-1.5 flex items-center justify-between px-1 text-[11px] text-muted-foreground"
      >
        <span>
          {disabled && disabledHint ? (
            <span className="text-muted-foreground" aria-live="polite">
              {disabledHint}
            </span>
          ) : (
            <>
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
            </>
          )}
        </span>
        <span aria-live="polite" aria-atomic="true">
          {charCount > 0 && `${charCount.toLocaleString()} / ${MAX_PROMPT_CHARS.toLocaleString()}`}
        </span>
      </div>
    </div>
  );
});
