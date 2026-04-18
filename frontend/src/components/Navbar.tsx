import { Link, useLocation, useNavigate } from "react-router-dom";
import { useState, useEffect, useRef } from "react";
import { Menu, X, User, LogOut, CreditCard, Settings, ShieldCheck } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";
import { Logo } from "@/components/Logo";
import { ThemeToggle } from "@/components/ThemeToggle";

const navLinks = [
  { label: "Research", href: "/research" },
  { label: "Mariana", href: "/mariana" },
  { label: "Pricing", href: "/pricing" },
  { label: "Contact", href: "/contact" },
];

export function Navbar() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, logout } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 20);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  // Close user menu on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setUserMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  // Close mobile menu on route change
  useEffect(() => {
    setMobileOpen(false);
    setUserMenuOpen(false);
  }, [location.pathname]);

  // BUG-014: Navigate to / after logout for consistency with Account.tsx
  const handleLogout = async () => {
    await logout();
    navigate("/");
  };

  return (
    <nav
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-300 ${
        scrolled
          ? "bg-background/80 backdrop-blur-xl border-b border-border/50 shadow-sm"
          : "bg-transparent"
      }`}
    >
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
        <Link to="/" className="flex items-center gap-2">
          <Logo size="sm" />
        </Link>

        {/* Desktop */}
        <div className="hidden items-center gap-1 md:flex">
          {navLinks.map((link) => (
            <Link
              key={link.href}
              to={link.href}
              className={`rounded-md px-3 py-2 text-sm font-medium transition-colors ${
                location.pathname === link.href
                  ? "text-foreground bg-secondary"
                  : "text-muted-foreground hover:text-foreground hover:bg-secondary/50"
              }`}
            >
              {link.label}
            </Link>
          ))}

          <div className="mx-2 h-4 w-px bg-border" />

          <ThemeToggle />

          {user ? (
            <div className="relative ml-1" ref={menuRef}>
              <button
                onClick={() => setUserMenuOpen(!userMenuOpen)}
                aria-expanded={userMenuOpen}
                aria-haspopup="menu"
                onKeyDown={(e) => e.key === "Escape" && setUserMenuOpen(false)}
                className="flex h-8 items-center gap-2 rounded-md bg-secondary px-3 text-sm font-medium text-foreground transition-colors hover:bg-secondary/80"
              >
                <User size={14} />
                <span>{user.name}</span>
              </button>
              {userMenuOpen && (
                <div role="menu" className="absolute right-0 top-full mt-2 w-56 rounded-lg border border-border bg-card py-1 shadow-lg">
                  <Link role="menuitem" to="/chat" className="flex items-center gap-2 px-4 py-2.5 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors">
                    Research
                  </Link>
                  <Link role="menuitem" to="/account" className="flex items-center gap-2 px-4 py-2.5 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors">
                    <Settings size={13} /> Account
                  </Link>
                  <Link role="menuitem" to="/checkout" className="flex items-center gap-2 px-4 py-2.5 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors">
                    <CreditCard size={13} /> Upgrade plan
                  </Link>
                  {user.role === "admin" && (
                    <Link role="menuitem" to="/admin" className="flex items-center gap-2 px-4 py-2.5 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors">
                      <ShieldCheck size={13} /> Admin
                    </Link>
                  )}
                  <div className="mx-3 my-1 border-t border-border" />
                  <div className="px-4 py-2">
                    <p className="text-xs text-muted-foreground">
                      {user.tokens.toLocaleString()} credits
                    </p>
                  </div>
                  <button
                    role="menuitem"
                    onClick={handleLogout}
                    className="flex w-full items-center gap-2 px-4 py-2.5 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
                  >
                    <LogOut size={13} /> Sign out
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-center gap-2 ml-1">
              <Link
                to="/login"
                className="rounded-md px-3 py-2 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground hover:bg-secondary/50"
              >
                Log in
              </Link>
              <Link
                to="/signup"
                className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground transition-all hover:opacity-90 shadow-sm hover:shadow-md"
              >
                Get Started
              </Link>
            </div>
          )}
        </div>

        {/* Mobile toggle */}
        <div className="flex items-center gap-2 md:hidden">
          <ThemeToggle />
          <button
            onClick={() => setMobileOpen(!mobileOpen)}
            className="flex h-8 w-8 items-center justify-center rounded-md text-foreground hover:bg-secondary"
            aria-label="Toggle menu"
          >
            {mobileOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
        </div>
      </div>

      {/* Mobile menu */}
      <div
        className={`overflow-hidden transition-all duration-300 md:hidden ${
          mobileOpen ? "max-h-[28rem] border-t border-border" : "max-h-0"
        }`}
      >
        <div className="bg-background/95 backdrop-blur-xl px-6 py-4">
          <div className="flex flex-col gap-1">
            {navLinks.map((link) => (
              <Link
                key={link.href}
                to={link.href}
                className={`rounded-md px-3 py-2.5 text-sm font-medium transition-colors ${
                  location.pathname === link.href
                    ? "text-foreground bg-secondary"
                    : "text-muted-foreground hover:text-foreground hover:bg-secondary/50"
                }`}
              >
                {link.label}
              </Link>
            ))}

            <div className="my-2 border-t border-border" />

            {user ? (
              <>
                <Link to="/chat" className="rounded-md px-3 py-2.5 text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-secondary/50">Research</Link>
                <Link to="/account" className="rounded-md px-3 py-2.5 text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-secondary/50">Account</Link>
                <Link to="/checkout" className="rounded-md px-3 py-2.5 text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-secondary/50">Upgrade plan</Link>
                {user.role === "admin" && (
                  <Link to="/admin" className="rounded-md px-3 py-2.5 text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-secondary/50">Admin</Link>
                )}
                <div className="my-2 border-t border-border" />
                <p className="px-3 text-xs text-muted-foreground">
                  {user.name} · {user.tokens.toLocaleString()} credits
                </p>
                <button
                  onClick={handleLogout}
                  className="rounded-md px-3 py-2.5 text-left text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-secondary/50"
                >
                  Sign out
                </button>
              </>
            ) : (
              <>
                <Link to="/login" className="rounded-md px-3 py-2.5 text-sm font-medium text-muted-foreground hover:text-foreground hover:bg-secondary/50">Log in</Link>
                <Link
                  to="/signup"
                  className="mt-2 rounded-md bg-primary px-4 py-2.5 text-center text-sm font-medium text-primary-foreground"
                >
                  Get Started
                </Link>
              </>
            )}
          </div>
        </div>
      </div>
    </nav>
  );
}
