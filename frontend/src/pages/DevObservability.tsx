/**
 * Dev-only Observability preview.
 *
 * Exercises:
 *   - errorToast() with an ApiError (carries request_id, shows Report issue).
 *   - errorToast() with a plain Error (no request_id).
 *   - Synthetic captureError flow.
 *   - Sentry-style breadcrumb buffer dump.
 *   - "Crash render" button to trigger the AppErrorBoundary fallback.
 *
 * Gated on import.meta.env.DEV in App.tsx — never ships to production.
 */
import { useState } from "react";
import { Navbar } from "@/components/Navbar";
import { errorToast } from "@/lib/errorToast";
import {
  addBreadcrumb,
  captureError,
  getBreadcrumbs,
  buildReportIssueUrl,
} from "@/lib/observability";
import { ApiError } from "@/lib/api";

function CrashOnRender() {
  // Throw during render to exercise componentDidCatch.
  throw new Error("synthetic render crash from /dev/observability");
}

export default function DevObservability() {
  const [crash, setCrash] = useState(false);
  const [breadcrumbsTick, setBreadcrumbsTick] = useState(0);

  const triggerApiToast = () => {
    const err = new ApiError(
      503,
      "Backend unavailable. Try again in a few seconds.",
      null,
      "req_4f9c8b2e1a6d4e72b8d45f0c3e4a90af",
    );
    addBreadcrumb({
      category: "ui",
      message: "user clicked: api error toast",
    });
    errorToast(err, {
      title: "Could not start checkout",
      surface: "billing.checkout",
    });
    setBreadcrumbsTick((t) => t + 1);
  };

  const triggerPlainToast = () => {
    addBreadcrumb({ category: "ui", message: "user clicked: plain error toast" });
    errorToast(new Error("Something locally went sideways."), {
      title: "Could not save",
      surface: "demo.plain",
    });
    setBreadcrumbsTick((t) => t + 1);
  };

  const triggerCaptureOnly = () => {
    captureError(new RangeError("synthetic background error"), {
      surface: "demo.capture-only",
      data: { example: true },
    });
    setBreadcrumbsTick((t) => t + 1);
  };

  if (crash) return <CrashOnRender />;

  const reportUrl = buildReportIssueUrl({
    subject: "[Deft] Dev observability preview",
    surface: "demo.preview",
    requestId: "req_preview",
  });

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <Navbar />
      <div className="mt-16 border-b border-border/60 bg-surface-1/40 px-3 py-2">
        <div className="mx-auto flex max-w-[1100px] items-center gap-2 text-[11px] text-muted-foreground">
          <span className="font-mono uppercase tracking-[0.16em]">
            /dev/observability
          </span>
          <span aria-hidden>·</span>
          <span>P17 — error capture + report issue + breadcrumbs</span>
        </div>
      </div>

      <main className="mx-auto w-full max-w-[1100px] px-6 py-12">
        <h1 className="font-serif text-3xl font-semibold">Observability</h1>
        <p className="mt-2 max-w-prose text-sm leading-6 text-muted-foreground">
          Every user-visible failure should carry a request id, a Report issue
          link, and a breadcrumb tail. This page exercises that path so we can
          eyeball it at a glance.
        </p>

        <section className="mt-8 grid gap-4 sm:grid-cols-2">
          <Card title="Toast with request_id" subtitle="Carries x-request-id from ApiError">
            <button
              type="button"
              onClick={triggerApiToast}
              className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Trigger 503 toast
            </button>
          </Card>

          <Card title="Toast without request_id" subtitle="Plain Error (no API origin)">
            <button
              type="button"
              onClick={triggerPlainToast}
              className="rounded-md border border-border bg-surface-2/40 px-4 py-2 text-sm font-medium hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Trigger plain toast
            </button>
          </Card>

          <Card title="Capture without toast" subtitle="Background error with breadcrumbs only">
            <button
              type="button"
              onClick={triggerCaptureOnly}
              className="rounded-md border border-border bg-surface-2/40 px-4 py-2 text-sm font-medium hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              captureError()
            </button>
          </Card>

          <Card title="Crash the renderer" subtitle="Exercises AppErrorBoundary fallback">
            <button
              type="button"
              onClick={() => setCrash(true)}
              className="rounded-md border border-destructive/60 bg-destructive/10 px-4 py-2 text-sm font-medium text-destructive hover:bg-destructive/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Throw on next render
            </button>
          </Card>
        </section>

        <section className="mt-8">
          <h2 className="text-sm font-medium uppercase tracking-[0.14em] text-muted-foreground">
            Pre-filled report issue
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            The Report issue link in toasts and the AppErrorBoundary fallback
            opens a mailto with breadcrumbs, route, release, and request id
            attached.
          </p>
          <a
            href={reportUrl}
            className="mt-3 inline-flex rounded-md border border-border bg-surface-2/40 px-3 py-1.5 text-xs font-medium text-foreground hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Open prefilled mailto
          </a>
        </section>

        <section className="mt-8">
          <h2 className="text-sm font-medium uppercase tracking-[0.14em] text-muted-foreground">
            Breadcrumb buffer (last 50)
          </h2>
          <p className="mt-2 text-xs text-muted-foreground">
            buffered: {getBreadcrumbs().length} entries · refresh #{breadcrumbsTick}
          </p>
          <pre className="mt-3 max-h-[280px] overflow-auto rounded-md border border-border bg-surface-2/40 p-3 font-mono text-[11.5px] leading-5">
            {getBreadcrumbs()
              .map(
                (b) =>
                  `[${b.timestamp}] ${b.category} ${b.level ?? "info"} :: ${b.message}` +
                  (b.data ? ` ${JSON.stringify(b.data)}` : ""),
              )
              .join("\n") || "(empty — interact above to fill)"}
          </pre>
        </section>
      </main>
    </div>
  );
}

function Card({
  title,
  subtitle,
  children,
}: {
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg border border-border bg-card p-5">
      <h3 className="text-sm font-medium text-foreground">{title}</h3>
      <p className="mt-1 text-xs text-muted-foreground">{subtitle}</p>
      <div className="mt-4">{children}</div>
    </div>
  );
}
