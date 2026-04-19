import { Navigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";

/**
 * FE-CRIT-01 fix: Route-level auth guard that prevents protected pages from
 * rendering before authentication is resolved. Shows a spinner while auth is
 * loading, redirects to /login if unauthenticated, and only renders children
 * once a valid user session is confirmed.
 */
export default function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-background">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary" />
      </div>
    );
  }

  if (!user) {
    return <Navigate to="/login" replace />;
  }

  return <>{children}</>;
}
