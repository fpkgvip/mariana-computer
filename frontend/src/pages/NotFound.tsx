import { Link } from "react-router-dom";
import { Logo } from "@/components/Logo";

export default function NotFound() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center bg-background px-6 text-center">
      <Logo size="md" />
      <h1 className="mt-8 text-5xl font-bold text-foreground">404</h1>
      <p className="mt-3 text-base text-muted-foreground">
        This page doesn't exist.
      </p>
      <Link
        to="/"
        className="mt-6 rounded-lg bg-primary px-5 py-2.5 text-sm font-semibold text-primary-foreground shadow-md transition-all hover:opacity-90 hover:shadow-lg"
      >
        Back to home
      </Link>
    </div>
  );
}
