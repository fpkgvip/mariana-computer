import { Link } from "react-router-dom";
import { Logo } from "@/components/Logo";

const productLinks = [
  { label: "Research", href: "/research" },
  { label: "Mariana", href: "/mariana" },
  { label: "Pricing", href: "/pricing" },
];

const companyLinks = [
  { label: "Contact", href: "/contact" },
  { label: "Sign In", href: "/login" },
];

export function Footer() {
  return (
    <footer className="border-t border-border bg-card/50">
      <div className="mx-auto max-w-7xl px-6 py-12">
        <div className="grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
          {/* Brand */}
          <div className="sm:col-span-2 lg:col-span-2">
            <Logo size="sm" />
            <p className="mt-3 max-w-xs text-sm leading-relaxed text-muted-foreground">
              Autonomous deep financial research powered by frontier AI.
              An AI with its own computer.
            </p>
            <a
              href="mailto:support@mariana.co"
              className="mt-3 inline-block text-sm text-muted-foreground transition-colors hover:text-primary"
            >
              support@mariana.co
            </a>
          </div>

          {/* Product */}
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Product
            </p>
            <div className="mt-4 flex flex-col gap-2.5">
              {productLinks.map((link) => (
                <Link
                  key={link.href}
                  to={link.href}
                  className="text-sm text-muted-foreground transition-colors hover:text-foreground"
                >
                  {link.label}
                </Link>
              ))}
            </div>
          </div>

          {/* Company */}
          <div>
            <p className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Company
            </p>
            <div className="mt-4 flex flex-col gap-2.5">
              {companyLinks.map((link) => (
                <Link
                  key={link.href}
                  to={link.href}
                  className="text-sm text-muted-foreground transition-colors hover:text-foreground"
                >
                  {link.label}
                </Link>
              ))}
            </div>
          </div>
        </div>

        <div className="mt-10 border-t border-border pt-6">
          <p className="text-xs text-muted-foreground/60">
            &copy; {new Date().getFullYear()} Mariana. All rights reserved.
          </p>
        </div>
      </div>
    </footer>
  );
}
