import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCw, MessageSquareWarning } from "lucide-react";
import { buildReportIssueUrl, captureError } from "@/lib/observability";

interface AppErrorBoundaryProps {
  children: ReactNode;
}

interface AppErrorBoundaryState {
  hasError: boolean;
  /** Last error message — surfaced to the user as a hint. */
  errorMessage: string | null;
}

/**
 * Top-level error boundary for the whole app.
 *
 * Captures three classes of failure:
 *   1. Render-time exceptions (componentDidCatch)
 *   2. Uncaught window errors (window.onerror)
 *   3. Unhandled promise rejections (window.onunhandledrejection)
 *
 * Every captured error is forwarded to `captureError()` (Sentry-ready) with
 * a `surface: "app"` tag, so the full breadcrumb tail makes it into the
 * incident report.
 */
export default class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  state: AppErrorBoundaryState = { hasError: false, errorMessage: null };

  private readonly handleWindowError = (event: ErrorEvent): void => {
    const err = event.error ?? new Error(event.message);
    captureError(err, { surface: "app.window-error" });
    this.setState({ hasError: true, errorMessage: pickMessage(err) });
  };

  private readonly handleUnhandledRejection = (event: PromiseRejectionEvent): void => {
    // FE-HIGH-06 fix: Catch ALL unhandled rejections in the error boundary.
    // Network-related errors (AbortError, fetch failures) are still excluded
    // since they are transient and fire-and-forget by design.
    const reason = event.reason;
    if (reason instanceof DOMException && reason.name === "AbortError") return;
    if (reason instanceof Error && reason.message.includes("NetworkError")) return;
    if (reason instanceof Error && reason.message.includes("Failed to fetch")) return;
    captureError(reason, { surface: "app.unhandled-rejection" });
    this.setState({ hasError: true, errorMessage: pickMessage(reason) });
  };

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { hasError: true, errorMessage: pickMessage(error) };
  }

  componentDidMount(): void {
    window.addEventListener("error", this.handleWindowError);
    window.addEventListener("unhandledrejection", this.handleUnhandledRejection);
  }

  componentWillUnmount(): void {
    window.removeEventListener("error", this.handleWindowError);
    window.removeEventListener("unhandledrejection", this.handleUnhandledRejection);
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    captureError(error, {
      surface: "app.render",
      data: { componentStack: errorInfo.componentStack ?? null },
    });
  }

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (!this.state.hasError) {
      return this.props.children;
    }

    const reportUrl = buildReportIssueUrl({
      subject: "[Deft] App crashed",
      surface: "app",
    });

    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6 py-12">
        <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="rounded-full bg-red-500/10 p-2 text-red-400">
              <AlertTriangle size={20} aria-hidden />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-foreground">
                Something went wrong
              </h1>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                Deft hit an unexpected application error. Reload the page to restore the session.
              </p>
              {this.state.errorMessage && (
                <p className="mt-2 text-xs leading-5 text-muted-foreground/80 break-words">
                  <span className="font-mono">{truncate(this.state.errorMessage, 240)}</span>
                </p>
              )}
            </div>
          </div>

          <div className="mt-6 flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={this.handleReload}
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <RefreshCw size={14} aria-hidden />
              Reload page
            </button>
            <a
              href={reportUrl}
              className="inline-flex items-center gap-2 rounded-md border border-border bg-surface-2/40 px-4 py-2 text-sm font-medium text-foreground transition-colors hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              <MessageSquareWarning size={14} aria-hidden />
              Report issue
            </a>
          </div>
        </div>
      </div>
    );
  }
}

function pickMessage(err: unknown): string | null {
  if (err instanceof Error) return err.message || err.name;
  if (typeof err === "string") return err;
  if (err && typeof err === "object") {
    try {
      return JSON.stringify(err).slice(0, 240);
    } catch {
      return null;
    }
  }
  return null;
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}
