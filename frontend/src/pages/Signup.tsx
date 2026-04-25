import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { track } from "@/lib/analytics";

export default function Signup() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [pendingNav, setPendingNav] = useState(false);
  const { signup, user } = useAuth();
  const navigate = useNavigate();

  // BUG-R2C-12 fix: same race as Login — wait for the AuthContext user before
  // navigating, otherwise ProtectedRoute on /chat will bounce us back.
  useEffect(() => {
    if (pendingNav && user) {
      navigate("/chat");
    }
  }, [pendingNav, user, navigate]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // BUG-FE-136: Validate email format client-side before round trip
    const trimmedEmail = email.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(trimmedEmail)) {
      toast.error("Invalid email", { description: "Please enter a valid email address." });
      return;
    }

    // BUG-022: Client-side password length validation
    if (password.length < 8) {
      toast.error("Password too short", {
        description: "Password must be at least 8 characters.",
      });
      return;
    }

    setIsLoading(true);
    try {
      // BUG-FE-140: Use trimmed email
      const confirmed = await signup(trimmedEmail, name, password);
      // BUG-R2C-03: Only navigate to /chat when signup returned a live session
      // (email confirmation disabled). When email confirmation is required,
      // signup() returns false and the toast is already shown by AuthContext.
      if (confirmed) {
        try {
          track("signup_completed", { method: "password" });
        } catch {
          // ignore
        }
        setPendingNav(true);
        // Navigation runs from useEffect once AuthContext.user is populated.
      }
      // else: stay on page — user must confirm email first
    } catch {
      // Error toast already shown by AuthContext.signup()
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-sm">
        <Link to="/" className="mb-10 block text-lg font-semibold tracking-tight text-foreground">
          Deft
        </Link>

        <h1 className="text-2xl font-semibold tracking-tight text-foreground">Create an account</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Set a goal. Set a ceiling. Let Deft ship the work.
        </p>

        <form onSubmit={handleSubmit} className="mt-8 space-y-4">
          <div>
            <label htmlFor="name" className="mb-1.5 block text-xs font-medium text-muted-foreground">Name</label>
            <Input
              id="name"
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              required
              placeholder="Your name"
              disabled={isLoading}
            />
          </div>
          <div>
            <label htmlFor="email" className="mb-1.5 block text-xs font-medium text-muted-foreground">Email</label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              placeholder="you@firm.com"
              disabled={isLoading}
            />
          </div>
          <div>
            <label htmlFor="password" className="mb-1.5 block text-xs font-medium text-muted-foreground">Password</label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={8}
              placeholder="••••••••"
              disabled={isLoading}
            />
            {/* BUG-R1-15: Show minimum length hint before submission */}
            <p className="mt-1 text-xs text-muted-foreground">Minimum 8 characters</p>
          </div>

          <Button type="submit" className="w-full" disabled={isLoading}>
            {isLoading ? "Creating account…" : "Create account"}
          </Button>
        </form>

        <p className="mt-8 text-center text-xs text-muted-foreground">
          Already have an account?{" "}
          <Link to="/login" className="font-medium text-foreground hover:underline">Sign in</Link>
        </p>
      </div>
    </div>
  );
}
