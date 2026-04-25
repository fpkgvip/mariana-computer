/**
 * errorToast — a single helper that turns any caught error into a toast with
 * (a) a clear title, (b) a short description, (c) the request id when one is
 * known (visible + copyable), and (d) a "Report issue" action that opens a
 * pre-filled mail to support with breadcrumbs and the request id attached.
 *
 * Designed to make P17 adoption mechanical: callsites that previously did
 *   toast.error("X failed", { description: msg })
 * become
 *   errorToast(err, { title: "X failed", surface: "billing.checkout" })
 * and get the full diagnostic payload for free.
 */
import { toast } from "sonner";
import { ApiError } from "@/lib/api";
import {
  buildReportIssueUrl,
  captureError,
} from "@/lib/observability";

export interface ErrorToastOptions {
  /** Toast title. Defaults to "Something went wrong". */
  title?: string;
  /** Optional override description; defaults to the error message. */
  description?: string;
  /** A short string that names the surface (e.g. "billing.checkout"). */
  surface?: string;
  /** Suppress the captureError call (useful for known-soft errors). */
  silent?: boolean;
}

function pickRequestId(err: unknown): string | null {
  if (err instanceof ApiError) return err.requestId;
  return null;
}

function pickMessage(err: unknown, fallback: string): string {
  if (err instanceof Error) return err.message || fallback;
  if (typeof err === "string") return err;
  return fallback;
}

/**
 * Surface an error to the user as a toast and (unless silent) capture it
 * to observability with the request id attached.
 *
 * Returns the (truncated) request id so callers can also surface it
 * inline if they want.
 */
export function errorToast(err: unknown, opts: ErrorToastOptions = {}): string | null {
  const title = opts.title ?? "Something went wrong";
  const description = opts.description ?? pickMessage(err, "Please try again.");
  const requestId = pickRequestId(err);
  const reportUrl = buildReportIssueUrl({
    subject: `[Deft] ${title}`,
    requestId,
    surface: opts.surface,
  });

  // Description string includes the short request id so the user sees a
  // copyable handle even without expanding the toast.
  const desc =
    requestId !== null
      ? `${description}\nrequest_id: ${requestId.slice(0, 8)}`
      : description;

  toast.error(title, {
    description: desc,
    action: {
      label: "Report issue",
      onClick: () => {
        if (typeof window !== "undefined") {
          window.location.href = reportUrl;
        }
      },
    },
  });

  if (!opts.silent) {
    captureError(err, { surface: opts.surface, requestId });
  }

  return requestId;
}
