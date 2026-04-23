import { Link, useLocation, useNavigate } from "react-router-dom";
import { useState, useEffect, useRef } from "react";
import { Menu, X, User, LogOut, CreditCard, Settings, ShieldCheck, Inbox } from "lucide-react";
import { useAuth } from "@/contexts/AuthContext";

const navLinks = [
  { label: "Product", href: "/mariana" },
  { label: "Examples", href: "/research" },
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
      className={`fixed top-0 left-0 right-0 z-50 transition-all duration-500 ${
        scrolled
          ? "bg-background/80 backdrop-blur-xl shadow-[0_1px_0_0_hsl(var(--border))]"
          : "bg-transparent"
      }`}
    >
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-6">
        <Link to="/" className="flex items-center gap-2">
          <span className="font-serif text-lg font-semibold tracking-tight text-foreground">
            Mariana
          </span>
        </Link>

        {/* Desktop */}
        <div className="hidden items-center gap-8 md:flex">
          {navLinks.map((link) => (
            <Link
              key={link.href}
              to={link.href}
              className={`text-[13px] font-medium transition-colors ${
                location.pathname === link.href
                  ? "text-foreground"
                  : "text-muted-foreground hover:text-foreground"
              }`}
            >
              {link.label}
            </Link>
          ))}

          {user ? (
            <div className="relative" ref={menuRef}>
              <button
                onClick={() => setUserMenuOpen(!userMenuOpen)}
                aria-expanded={userMenuOpen}
                aria-haspopup="menu"
                onKeyDown={(e) => e.key === "Escape" && setUserMenuOpen(false)}
                className="flex items-center gap-2 text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground"
              >
                <User size={15} />
                <span>{user.name}</span>
              </button>
              {userMenuOpen && (
                <div role="menu" className="absolute right-0 top-full mt-2 w-56 rounded-lg border border-border bg-card py-1 shadow-lg">
                  <Link role="menuitem" to="/chat" className="flex items-center gap-2 px-4 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground">
                    Workspace
                  </Link>
                  <Link role="menuitem" to="/tasks" className="flex items-center gap-2 px-4 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground">
                    <Inbox size={13} /> Tasks
                  </Link>
                  <Link role="menuitem" to="/account" className="flex items-center gap-2 px-4 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground">
                    <Settings size={13} /> Account
                  </Link>
                  <Link role="menuitem" to="/checkout" className="flex items-center gap-2 px-4 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground">
                    <CreditCard size={13} /> Upgrade plan
                  </Link>
                  {user.role === "admin" && (
                    <Link role="menuitem" to="/admin" className="flex items-center gap-2 px-4 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground">
                      <ShieldCheck size={13} /> Admin
                    </Link>
                  )}
                  <div className="mx-4 my-1 border-t border-border" />
                  <div className="px-4 py-2">
                    <p className="text-xs text-muted-foreground/60">
                      {user.tokens.toLocaleString()} credits
                    </p>
                  </div>
                  <button
                    role="menuitem"
                    onClick={handleLogout}
                    className="flex w-full items-center gap-2 px-4 py-2 text-sm text-muted-foreground hover:bg-secondary hover:text-foreground"
                  >
                    <LogOut size={13} /> Sign out
                  </button>
                </div>
              )}
            </div>
          ) : (
            <div className="flex items-center gap-4">
              <Link
                to="/login"
                className="text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground"
              >
                Log in
              </Link>
              <Link
                to="/signup"
                className="rounded-md bg-primary px-4 py-2 text-[13px] font-medium text-primary-foreground transition-all hover:bg-primary/90 hover:shadow-lg hover:shadow-primary/10"
              >
                Try Mariana
              </Link>
            </div>
          )}
        </div>

        {/* Mobile toggle */}
        <button
          onClick={() => setMobileOpen(!mobileOpen)}
          className="text-foreground md:hidden"
          aria-label="Toggle menu"
        >
          {mobileOpen ? <X size={20} /> : <Menu size={20} />}
        </button>
      </div>

      {/* Mobile menu */}
      <div
        className={`overflow-hidden transition-all duration-300 md:hidden ${
          mobileOpen ? "max-h-[28rem] border-t border-border" : "max-h-0"
        }`}
      >
        <div className="bg-background px-6 py-4">
          <div className="flex flex-col gap-3">
            {navLinks.map((link) => (
              <Link
                key={link.href}
                to={link.href}
                className={`py-1 text-sm ${
                  location.pathname === link.href
                    ? "text-foreground font-medium"
                    : "text-muted-foreground"
                }`}
              >
                {link.label}
              </Link>
            ))}

            <div className="my-1 border-t border-border" />

            {user ? (
              <>
                <Link to="/account" className="py-1 text-sm text-muted-foreground">Account</Link>
                <Link to="/checkout" className="py-1 text-sm text-muted-foreground">Upgrade plan</Link>
                <Link to="/chat" className="py-1 text-sm text-muted-foreground">Workspace</Link>
                <Link to="/tasks" className="py-1 text-sm text-muted-foreground">Tasks</Link>
                {user.role === "admin" && (
                  <Link to="/admin" className="py-1 text-sm text-muted-foreground">Admin</Link>
                )}
                <div className="my-1 border-t border-border" />
                <p className="text-xs text-muted-foreground/60">
                  {user.name} · {user.tokens.toLocaleString()} credits
                </p>
                <button
                  onClick={handleLogout}
                  className="py-1 text-left text-sm text-muted-foreground"
                >
                  Sign out
                </button>
              </>
            ) : (
              <>
                <Link to="/login" className="py-1 text-sm text-muted-foreground">Log in</Link>
                <Link
                  to="/signup"
                  className="mt-1 rounded-md bg-primary px-4 py-2.5 text-center text-sm font-medium text-primary-foreground"
                >
                  Try Mariana
                </Link>
              </>
            )}
          </div>
        </div>
      </div>
    </nav>
  );
}
