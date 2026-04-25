import { Navigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Routes the bare "/" path:
 *  - Logged-in user → /build (the studio is home for signed-in users,
 *    Lovable-style; we never show marketing copy to a paying user).
 *  - Logged-out user → render the marketing landing page.
 *  - Auth still resolving → render the spinner already shown by AuthProvider
 *    by returning null here so we don't flash either page.
 */
export default function LandingGate({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();

  // AuthProvider itself renders a fullscreen spinner during initial load,
  // so by the time we get here `loading` is false.  Defensive null fallback.
  if (loading) return null;
  if (user) return <Navigate to="/build" replace />;
  return <>{children}</>;
}
