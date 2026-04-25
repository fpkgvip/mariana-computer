/**
 * Per-route error boundary used to wrap the body of every <Route> so a crash
 * inside one page does not unmount the rest of the shell (Navbar, Toaster,
 * Auth context, etc.). Falls back to a friendly recoverable card.
 */
import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCw, ArrowLeft } from "lucide-react";
import { Link } from "react-router-dom";

interface Props {
  children: ReactNode;
  /** Optional friendly route label shown in the error UI. */
  routeName?: string;
}

interface State {
  error: Error | null;
}

export default class RouteErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error(
      `[RouteErrorBoundary] crash in ${this.props.routeName ?? "route"}:`,
      error,
      info,
    );
  }

  private readonly handleRetry = (): void => {
    this.setState({ error: null });
  };

  render(): ReactNode {
    if (!this.state.error) return this.props.children;

    return (
      <div
        role="alert"
        aria-live="assertive"
        className="flex min-h-[50vh] items-center justify-center px-6 py-12"
      >
        <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 shadow-sm">
          <div className="flex items-start gap-3">
            <div className="rounded-full bg-red-500/10 p-2 text-red-400">
              <AlertTriangle size={20} aria-hidden="true" />
            </div>
            <div className="flex-1">
              <h2 className="text-lg font-semibold text-foreground">
                Something went wrong
              </h2>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">
                {this.props.routeName ? `The ${this.props.routeName} page` : "This page"}{" "}
                hit an unexpected error. The rest of the app is still working.
              </p>
              <pre className="mt-3 max-h-32 overflow-auto rounded-md bg-muted px-3 py-2 text-xs text-foreground">
                {this.state.error.message || "Unknown error"}
              </pre>
              <div className="mt-4 flex gap-2">
                <button
                  type="button"
                  onClick={this.handleRetry}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-2 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
                >
                  <RefreshCw size={14} aria-hidden="true" /> Try again
                </button>
                <Link
                  to="/"
                  className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-secondary"
                >
                  <ArrowLeft size={14} aria-hidden="true" /> Home
                </Link>
              </div>
            </div>
          </div>
        </div>
      </div>
    );
  }
}
