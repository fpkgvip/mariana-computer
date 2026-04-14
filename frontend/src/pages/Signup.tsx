import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAuth } from "@/contexts/AuthContext";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";

export default function Signup() {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const { signup } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();

    // BUG-022: Client-side password length validation
    if (password.length < 8) {
      toast.error("Password too short", {
        description: "Password must be at least 8 characters.",
      });
      return;
    }

    setIsLoading(true);
    try {
      await signup(email, name, password);
      // BUG-R1-01: Navigate unconditionally after a non-throwing signup().
      // Checking the `user` context value here is wrong — React state updates
      // from AuthContext.setUser() are batched and the closure still holds the
      // old (null) reference. If email confirmation is required, AuthContext
      // will have left user=null and Chat.tsx will redirect back to /login.
      navigate("/chat");
    } catch {
      // Error toast already shown by AuthContext.signup()
    } finally {
      setIsLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-background px-6">
      <div className="w-full max-w-sm">
        <Link to="/" className="mb-10 block font-serif text-lg font-semibold text-foreground">
          Mariana
        </Link>

        <h1 className="font-serif text-2xl font-semibold text-foreground">Create an account</h1>
        <p className="mt-2 text-sm text-muted-foreground">
          Start investigating with Mariana.
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
