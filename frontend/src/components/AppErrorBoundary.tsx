import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

interface AppErrorBoundaryProps {
  children: ReactNode;
}

interface AppErrorBoundaryState {
  hasError: boolean;
}

export default class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  state: AppErrorBoundaryState = { hasError: false };

  private readonly handleWindowError = (event: ErrorEvent): void => {
    console.error("[AppErrorBoundary] Uncaught window error:", event.error ?? event.message);
    this.setState({ hasError: true });
  };

  private readonly handleUnhandledRejection = (event: PromiseRejectionEvent): void => {
    console.error("[AppErrorBoundary] Unhandled promise rejection:", event.reason);
    // FE-HIGH-06 fix: Catch ALL unhandled rejections in the error boundary.
    // Previously only TypeError/ReferenceError/SyntaxError triggered the fallback,
    // which meant other fatal errors (e.g., RangeError, custom errors) would crash
    // silently. Network-related errors (AbortError, fetch failures) are still
    // excluded since they are transient and fire-and-forget by design.
    const reason = event.reason;
    if (reason instanceof DOMException && reason.name === "AbortError") {
      return; // Intentional fetch cancellations — not a crash
    }
    if (reason instanceof Error && reason.message.includes("NetworkError")) {
      return; // Transient network failures — not a crash
    }
    if (reason instanceof Error && reason.message.includes("Failed to fetch")) {
      return; // Transient fetch failures (offline, DNS, etc.)
    }
    this.setState({ hasError: true });
  };

  static getDerivedStateFromError(): AppErrorBoundaryState {
    return { hasError: true };
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
    console.error("[AppErrorBoundary] Uncaught render error:", error, errorInfo);
  }

  private handleReload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    if (!this.state.hasError) {
      return this.props.children;
    }

    return (
      <div className="flex min-h-screen items-center justify-center bg-background px-6 py-12">
        <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="rounded-full bg-red-500/10 p-2 text-red-400">
              <AlertTriangle size={20} />
            </div>
            <div>
              <h1 className="text-lg font-semibold text-foreground">Something went wrong</h1>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                Mariana hit an unexpected application error. Reload the page to restore the session.
              </p>
            </div>
          </div>

          <button
            type="button"
            onClick={this.handleReload}
            className="mt-6 inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
          >
            <RefreshCw size={14} />
            Reload page
          </button>
        </div>
      </div>
    );
  }
}
