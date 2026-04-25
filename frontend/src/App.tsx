import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Toaster as Sonner } from "@/components/ui/sonner";
import { Toaster } from "@/components/ui/toaster";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AuthProvider } from "@/contexts/AuthContext";
import AppErrorBoundary from "@/components/AppErrorBoundary";
import RouteErrorBoundary from "@/components/RouteErrorBoundary";
import ProtectedRoute from "@/components/ProtectedRoute";
import LandingGate from "@/components/LandingGate";
import { initAnalytics } from "@/lib/analytics";
import { initObservability, addBreadcrumb } from "@/lib/observability";
import { lazy, Suspense, useEffect } from "react";
import { supabaseConfigError } from "@/lib/supabase";
import { BRAND } from "@/lib/brand";
import { AlertTriangle, Loader2 } from "lucide-react";

// Eager-imported routes: highest-traffic entry points where any extra
// network hop after initial paint hurts perceived speed.
import Index from "./pages/Index";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import NotFound from "./pages/NotFound";

// Lazy-imported routes: every authenticated app surface and the rest of
// the marketing site. Each becomes its own chunk so the initial bundle
// only ships what's needed for the landing render.
//
// retryImport: dynamic-import retry wrapper. Two failure modes we care about:
//   1. transient network blip mid-fetch (retry once after a small delay).
//   2. chunk hash invalidated by a fresh deploy (the file no longer exists).
//      In that case the second attempt also fails — reload the page so the
//      browser pulls the new index.html and asset map. The session-flag guard
//      prevents a reload loop if the failure is something else.
function retryImport<T>(loader: () => Promise<T>, name: string): Promise<T> {
  return loader().catch((err) => {
    addBreadcrumb({ category: "chunk", message: `retry ${name}`, level: "warning" });
    return new Promise<T>((resolve, reject) => {
      setTimeout(() => {
        loader().then(resolve).catch((err2) => {
          const key = `chunk-reload:${name}`;
          if (typeof sessionStorage !== "undefined" && !sessionStorage.getItem(key)) {
            sessionStorage.setItem(key, "1");
            addBreadcrumb({ category: "chunk", message: `reload after ${name} failure`, level: "error" });
            window.location.reload();
            return;
          }
          reject(err2 ?? err);
        });
      }, 400);
    });
  });
}

const Research = lazy(() => retryImport(() => import("./pages/Research"), "Research"));
const Product = lazy(() => retryImport(() => import("./pages/Product"), "Product"));
const Chat = lazy(() => retryImport(() => import("./pages/Chat"), "Chat"));
const Build = lazy(() => retryImport(() => import("./pages/Build"), "Build"));
const Pricing = lazy(() => retryImport(() => import("./pages/Pricing"), "Pricing"));
const Contact = lazy(() => retryImport(() => import("./pages/Contact"), "Contact"));
const Checkout = lazy(() => retryImport(() => import("./pages/Checkout"), "Checkout"));
const Account = lazy(() => retryImport(() => import("./pages/Account"), "Account"));
const Admin = lazy(() => retryImport(() => import("./pages/Admin"), "Admin"));
const ResetPassword = lazy(() => retryImport(() => import("./pages/ResetPassword"), "ResetPassword"));
const Skills = lazy(() => retryImport(() => import("./pages/Skills"), "Skills"));
const InvestigationGraph = lazy(() => retryImport(() => import("./pages/InvestigationGraph"), "InvestigationGraph"));
const Tasks = lazy(() => retryImport(() => import("./pages/Tasks"), "Tasks"));
const TaskDetail = lazy(() => retryImport(() => import("./pages/TaskDetail"), "TaskDetail"));
const Vault = lazy(() => retryImport(() => import("./pages/Vault"), "Vault"));
const DevStudio = lazy(() => retryImport(() => import("./pages/DevStudio"), "DevStudio"));
const DevAccount = lazy(() => retryImport(() => import("./pages/DevAccount"), "DevAccount"));
const DevVault = lazy(() => retryImport(() => import("./pages/DevVault"), "DevVault"));
const DevProjects = lazy(() => retryImport(() => import("./pages/DevProjects"), "DevProjects"));
const DevStates = lazy(() => retryImport(() => import("./pages/DevStates"), "DevStates"));
const DevObservability = lazy(() => retryImport(() => import("./pages/DevObservability"), "DevObservability"));

// Suspense fallback shown during chunk fetch. Kept minimal and
// motion-respectful so users on slow connections see a stable surface
// instead of a blank screen.
const RouteFallback = () => (
  <div
    role="status"
    aria-live="polite"
    aria-label="Loading page"
    className="flex min-h-screen items-center justify-center bg-background"
  >
    <Loader2 size={20} className="motion-safe:animate-spin text-muted-foreground" aria-hidden />
    <span className="sr-only">Loading page</span>
  </div>
);

// BUG-FE-134 fix: Configure sensible defaults so react-query doesn't refetch
// aggressively on every window focus or mount. staleTime = 30s keeps data fresh
// for a reasonable window; refetchOnWindowFocus is disabled to prevent flicker
// and wasted API calls when users briefly switch tabs.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      staleTime: 30_000,
    },
  },
});

/**
 * BUG-029: BrowserRouter now wraps AuthProvider so AuthProvider (and any
 * future consumers within it) can use React Router hooks (useNavigate etc.).
 *
 * BUG-010: Added /reset-password route so the password-reset email link
 * from Login.tsx actually lands on a page instead of the 404.
 */
// BUG-FE-132 fix: If Supabase env vars are missing, render a friendly
// configuration error screen instead of blanking out the app. This used to
// throw at module import time, which the ErrorBoundary could not catch.
const ConfigErrorScreen = ({ message }: { message: string }) => (
  <div className="flex min-h-screen items-center justify-center bg-background px-6 py-12">
    <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 shadow-sm">
      <div className="flex items-start gap-3">
        <div className="rounded-full bg-red-500/10 p-2 text-red-400">
          <AlertTriangle size={20} />
        </div>
        <div>
          <h1 className="text-lg font-semibold text-foreground">Configuration error</h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {BRAND.name} could not start because a required environment variable is missing.
          </p>
          <pre className="mt-3 rounded-md bg-muted px-3 py-2 text-xs text-foreground overflow-x-auto">
            {message}
          </pre>
          <p className="mt-3 text-xs text-muted-foreground">
            Check the deployment configuration and ensure Supabase credentials are set.
          </p>
        </div>
      </div>
    </div>
  </div>
);

const App = () => {
  if (supabaseConfigError) {
    return <ConfigErrorScreen message={supabaseConfigError} />;
  }
  // Initialise analytics once at app boot. No-op when VITE_POSTHOG_KEY unset.
  // Wrapped in useEffect to keep rendering pure in StrictMode.
  // eslint-disable-next-line react-hooks/rules-of-hooks
  useEffect(() => {
    initAnalytics();
    initObservability();
    addBreadcrumb({
      category: "navigation",
      message: `app boot ${window.location.pathname}`,
    });
  }, []);
  return (
  <AppErrorBoundary>
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        <Toaster />
        <Sonner />
        <BrowserRouter>
          <AuthProvider>
            {/* No onboarding modal — Deft teaches by doing. The first prompt
               IS the onboarding (see Index hero + Build empty state). */}
            <Suspense fallback={<RouteFallback />}>
              <Routes>
                <Route
                  path="/"
                  element={
                    <RouteErrorBoundary routeName="home">
                      <LandingGate>
                        <Index />
                      </LandingGate>
                    </RouteErrorBoundary>
                  }
                />
                <Route path="/research" element={<RouteErrorBoundary routeName="research"><Research /></RouteErrorBoundary>} />
                <Route path="/product" element={<RouteErrorBoundary routeName="product"><Product /></RouteErrorBoundary>} />
                {/* Legacy /mariana → /product redirect (rebrand v1.0) */}
                <Route path="/mariana" element={<Navigate to="/product" replace />} />
                <Route path="/chat" element={<ProtectedRoute><RouteErrorBoundary routeName="chat"><Chat /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/build" element={<ProtectedRoute><RouteErrorBoundary routeName="build"><Build /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/studio" element={<Navigate to="/build" replace />} />
                <Route path="/pricing" element={<RouteErrorBoundary routeName="pricing"><Pricing /></RouteErrorBoundary>} />
                <Route path="/contact" element={<RouteErrorBoundary routeName="contact"><Contact /></RouteErrorBoundary>} />
                <Route path="/checkout" element={<ProtectedRoute><RouteErrorBoundary routeName="checkout"><Checkout /></RouteErrorBoundary></ProtectedRoute>} />
                {/* BUG-legacy: /buy-credits now redirects to /checkout for backward compat */}
                <Route path="/buy-credits" element={<ProtectedRoute><RouteErrorBoundary routeName="checkout"><Checkout /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/account" element={<ProtectedRoute><RouteErrorBoundary routeName="account"><Account /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/skills" element={<ProtectedRoute><RouteErrorBoundary routeName="skills"><Skills /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/graph" element={<ProtectedRoute><RouteErrorBoundary routeName="graph"><InvestigationGraph /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/graph/:taskId" element={<ProtectedRoute><RouteErrorBoundary routeName="graph"><InvestigationGraph /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/tasks" element={<ProtectedRoute><RouteErrorBoundary routeName="tasks"><Tasks /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/tasks/:taskId" element={<ProtectedRoute><RouteErrorBoundary routeName="task detail"><TaskDetail /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/vault" element={<ProtectedRoute><RouteErrorBoundary routeName="vault"><Vault /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/admin" element={<ProtectedRoute><RouteErrorBoundary routeName="admin"><Admin /></RouteErrorBoundary></ProtectedRoute>} />
                <Route path="/login" element={<RouteErrorBoundary routeName="login"><Login /></RouteErrorBoundary>} />
                <Route path="/signup" element={<RouteErrorBoundary routeName="signup"><Signup /></RouteErrorBoundary>} />
                <Route path="/reset-password" element={<RouteErrorBoundary routeName="password reset"><ResetPassword /></RouteErrorBoundary>} />
                {import.meta.env.DEV && (
                  <Route path="/dev/studio" element={<RouteErrorBoundary routeName="dev studio"><DevStudio /></RouteErrorBoundary>} />
                )}
                {import.meta.env.DEV && (
                  <Route path="/dev/account" element={<RouteErrorBoundary routeName="dev account"><DevAccount /></RouteErrorBoundary>} />
                )}
                {import.meta.env.DEV && (
                  <Route path="/dev/vault" element={<RouteErrorBoundary routeName="dev vault"><DevVault /></RouteErrorBoundary>} />
                )}
                {import.meta.env.DEV && (
                  <Route path="/dev/projects" element={<RouteErrorBoundary routeName="dev projects"><DevProjects /></RouteErrorBoundary>} />
                )}
                {import.meta.env.DEV && (
                  <Route path="/dev/states" element={<RouteErrorBoundary routeName="dev states"><DevStates /></RouteErrorBoundary>} />
                )}
                {import.meta.env.DEV && (
                  <Route path="/dev/observability" element={<RouteErrorBoundary routeName="dev observability"><DevObservability /></RouteErrorBoundary>} />
                )}
                <Route path="*" element={<NotFound />} />
              </Routes>
            </Suspense>
          </AuthProvider>
        </BrowserRouter>
      </TooltipProvider>
    </QueryClientProvider>
  </AppErrorBoundary>
  );
};

export default App;
