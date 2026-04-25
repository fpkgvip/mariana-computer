/**
 * Observability — breadcrumbs, error capture, request_id surfacing, and a
 * pre-filled "Report issue" link.
 *
 * Why this exists
 * ---------------
 * Sentry isn't yet enabled in production, but we behave _as if_ it is:
 *  - Every notable user action is captured as a breadcrumb in a bounded
 *    ring buffer.
 *  - Uncaught errors and `errorToast()` calls flush the buffer to
 *    `console.error` under a stable prefix (so a future Sentry SDK can be
 *    dropped in without changing call sites — the breadcrumb shape matches
 *    Sentry's).
 *  - When `VITE_SENTRY_DSN` is set, we lazily import `@sentry/browser`,
 *    init it, and forward every breadcrumb + error.  Bundles without
 *    the env var pay zero cost (the import is gated).
 *
 * Voice rule: never put hype words or emojis in user-visible strings.
 */

const MAX_BREADCRUMBS = 50;

export type BreadcrumbCategory =
  | "ui"
  | "navigation"
  | "auth"
  | "api"
  | "vault"
  | "billing"
  | "build"
  | "error";

export interface Breadcrumb {
  /** ISO 8601 timestamp. */
  timestamp: string;
  category: BreadcrumbCategory;
  message: string;
  /** Optional structured payload (must be JSON-serialisable). */
  data?: Record<string, unknown>;
  /** Sentry parity: "info" (default), "warning", "error". */
  level?: "info" | "warning" | "error";
}

const buffer: Breadcrumb[] = [];

let sentryReady = false;
// We keep this loose-typed so the bundle does not pull in @sentry/browser
// type signatures unless the DSN is present at build time.
let sentryClient: {
  addBreadcrumb: (b: unknown) => void;
  captureException: (err: unknown, ctx?: unknown) => void;
  setUser: (u: unknown) => void;
} | null = null;

/**
 * Initialise Sentry if `VITE_SENTRY_DSN` is set.  No-op otherwise.
 *
 * Must be invoked exactly once at app boot, after `initAnalytics()`.
 */
export async function initObservability(): Promise<void> {
  const dsn = import.meta.env.VITE_SENTRY_DSN as string | undefined;
  if (!dsn || sentryReady) return;
  try {
    // Runtime-only import keeps the dep out of bundles. The string is built
    // dynamically so Rollup does not attempt static resolution at build time.
    // If the package is not installed, this becomes a no-op.
    const moduleName = ["@sentry", "browser"].join("/");
    // eslint-disable-next-line @typescript-eslint/no-implied-eval, no-new-func
    const dynamicImport = new Function("m", "return import(m)") as (
      m: string,
    ) => Promise<unknown>;
    const sentry = (await dynamicImport(moduleName).catch(() => null)) as
      | (typeof sentryClient & {
          init: (cfg: Record<string, unknown>) => void;
        })
      | null;
    if (!sentry) return;
    sentry.init({
      dsn,
      tracesSampleRate: 0.0,
      release: (import.meta.env.VITE_RELEASE as string | undefined) ?? "dev",
      environment: (import.meta.env.MODE as string | undefined) ?? "production",
    });
    sentryClient = sentry as unknown as NonNullable<typeof sentryClient>;
    sentryReady = true;
    // Flush any breadcrumbs collected before init.
    for (const b of buffer) sentryClient?.addBreadcrumb(b);
  } catch (err) {
    // Never let observability break the app.
    // eslint-disable-next-line no-console
    console.warn("[observability] sentry init failed:", err);
  }
}

/** Identify the current user to Sentry (falls back to no-op when disabled). */
export function setUserContext(
  user: { id: string; email?: string } | null,
): void {
  if (!sentryClient) return;
  try {
    sentryClient.setUser(user);
  } catch {
    /* swallow */
  }
}

/**
 * Append a breadcrumb to the ring buffer.  Older entries are dropped once
 * the buffer fills.  Mirrored to Sentry when present.
 */
export function addBreadcrumb(b: Omit<Breadcrumb, "timestamp">): void {
  const entry: Breadcrumb = {
    timestamp: new Date().toISOString(),
    level: "info",
    ...b,
  };
  buffer.push(entry);
  if (buffer.length > MAX_BREADCRUMBS) buffer.shift();
  if (sentryClient) {
    try {
      sentryClient.addBreadcrumb(entry);
    } catch {
      /* swallow */
    }
  }
}

/** Returns a snapshot copy of the breadcrumb buffer for diagnostics / report-issue. */
export function getBreadcrumbs(): Breadcrumb[] {
  return buffer.slice();
}

/**
 * Capture an error.  Always logged to `console.error` under a stable prefix;
 * forwarded to Sentry when configured.  Adds a breadcrumb so subsequent
 * captures show the chain.
 */
export function captureError(
  err: unknown,
  context?: { surface?: string; requestId?: string | null; data?: Record<string, unknown> },
): void {
  const message = err instanceof Error ? err.message : String(err);
  addBreadcrumb({
    category: "error",
    level: "error",
    message,
    data: {
      surface: context?.surface,
      requestId: context?.requestId ?? null,
      ...(context?.data ?? {}),
    },
  });
  // eslint-disable-next-line no-console
  console.error(
    "[observability] captureError",
    { surface: context?.surface, requestId: context?.requestId, message },
    err,
  );
  if (sentryClient) {
    try {
      sentryClient.captureException(err, {
        tags: {
          surface: context?.surface ?? "unknown",
          request_id: context?.requestId ?? "none",
        },
        extra: context?.data,
      });
    } catch {
      /* swallow */
    }
  }
}

/**
 * Build a `mailto:` URL for the report-issue flow with the current app
 * version, route, breadcrumb tail, and (if known) request id pre-filled.
 *
 * Voice: short, plain, no hype.
 */
export function buildReportIssueUrl(args?: {
  subject?: string;
  requestId?: string | null;
  surface?: string;
}): string {
  const subject = args?.subject ?? "Issue with Deft";
  const route =
    typeof window !== "undefined" ? window.location.pathname + window.location.search : "/";
  const release = (import.meta.env.VITE_RELEASE as string | undefined) ?? "dev";
  const tail = buffer.slice(-10).map((b) => {
    const data = b.data ? ` ${safeJson(b.data)}` : "";
    return `[${b.timestamp}] ${b.category} ${b.level ?? "info"} :: ${b.message}${data}`;
  });
  const lines = [
    "Describe what you were doing when this happened:",
    "",
    "",
    "— diagnostic context, do not edit below —",
    `release: ${release}`,
    `route: ${route}`,
    `surface: ${args?.surface ?? "unknown"}`,
    `request_id: ${args?.requestId ?? "none"}`,
    `userAgent: ${typeof navigator !== "undefined" ? navigator.userAgent : "unknown"}`,
    "recent breadcrumbs:",
    ...tail,
  ];
  const body = encodeURIComponent(lines.join("\n"));
  return `mailto:support@deft.computer?subject=${encodeURIComponent(subject)}&body=${body}`;
}

function safeJson(value: unknown): string {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}
