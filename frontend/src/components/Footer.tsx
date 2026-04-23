import { Link } from "react-router-dom";
import { useAuth } from "../contexts/AuthContext";

const links = [
  { label: "Product", href: "/mariana" },
  { label: "Examples", href: "/research" },
  { label: "Pricing", href: "/pricing" },
  { label: "Contact", href: "/contact" },
];

export function Footer() {
  const { user } = useAuth();

  return (
    <footer className="border-t border-border">
      <div className="mx-auto max-w-7xl px-6 py-10">
        <div className="flex flex-col gap-8 md:flex-row md:items-start md:justify-between">
          {/* Brand */}
          <div>
            <Link to="/" className="font-serif text-sm font-semibold text-foreground">
              Mariana
            </Link>
            <p className="mt-1 text-xs text-muted-foreground">
              The AI that actually does the work.
            </p>
            <a href="mailto:support@mariana.co" className="mt-2 block text-xs text-muted-foreground transition-colors hover:text-foreground">
              support@mariana.co
            </a>
          </div>

          {/* Links */}
          <div className="flex flex-wrap gap-6 text-xs text-muted-foreground">
            {links.map((link) => (
              <Link key={link.href} to={link.href} className="transition-colors hover:text-foreground">
                {link.label}
              </Link>
            ))}
            {user ? (
              <Link to="/account" className="transition-colors hover:text-foreground">
                Account
              </Link>
            ) : (
              <Link to="/login" className="transition-colors hover:text-foreground">
                Sign In
              </Link>
            )}
          </div>
        </div>

        <div className="mt-8 border-t border-border pt-6">
          <p className="text-xs text-muted-foreground/50">
© {new Date().getFullYear()} Mariana. All rights reserved.
          </p>
        </div>
      </div>
    </footer>
  );
}
