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
import { useEffect } from "react";
import { supabaseConfigError } from "@/lib/supabase";
import { BRAND } from "@/lib/brand";
import { AlertTriangle } from "lucide-react";

import Index from "./pages/Index";
import Research from "./pages/Research";
import Product from "./pages/Product";
import Chat from "./pages/Chat";
import Build from "./pages/Build";
import Pricing from "./pages/Pricing";
import Contact from "./pages/Contact";
import Checkout from "./pages/Checkout";
import Account from "./pages/Account";
import Admin from "./pages/Admin";
import Login from "./pages/Login";
import Signup from "./pages/Signup";
import ResetPassword from "./pages/ResetPassword";
import Skills from "./pages/Skills";
import InvestigationGraph from "./pages/InvestigationGraph";
import Tasks from "./pages/Tasks";
import TaskDetail from "./pages/TaskDetail";
import Vault from "./pages/Vault";
import NotFound from "./pages/NotFound";
import DevStudio from "./pages/DevStudio";

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
  useEffect(() => { initAnalytics(); }, []);
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
              <Route path="*" element={<NotFound />} />
            </Routes>
          </AuthProvider>
        </BrowserRouter>
      </TooltipProvider>
    </QueryClientProvider>
  </AppErrorBoundary>
  );
};

export default App;
