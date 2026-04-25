import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";

/**
 * Route-level auth guard.
 *
 * - While the initial session is resolving, render a quiet spinner so we
 *   don't flash the public route at a logged-in user.
 * - When unauthenticated, send the visitor to /login and preserve the
 *   intended destination (path + query + hash) in `?next=`.  /login and
 *   /signup respect `next` and route the visitor there post-auth.  This
 *   makes the cross-page "prompt typed on the home page" flow lossless.
 */
export default function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-background">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary" />
      </div>
    );
  }

  if (!user) {
    const next = `${location.pathname}${location.search}${location.hash}`;
    const target = `/login?next=${encodeURIComponent(next)}`;
    return <Navigate to={target} replace />;
  }

  return <>{children}</>;
}
