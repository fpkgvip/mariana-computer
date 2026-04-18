import { useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { useAuth } from "@/contexts/AuthContext";
import { Mail } from "lucide-react";

/**
 * BUG-026 / BUG-030: The previous implementation collected card details
 * (card number, expiry, CVC) that were never validated or transmitted —
 * a fake payment flow using a setTimeout + local state update only.
 *
 * Replaced with a "contact us" page to request credits. Real payment
 * integration (Stripe etc.) must be wired on the backend before a payment
 * form should be re-introduced.
 */
export default function BuyCredits() {
  const { user } = useAuth();
  const navigate = useNavigate();

  // BUG-R1-10: Add a 500ms grace period before redirecting, matching Chat.tsx.
  useEffect(() => {
    if (!user) {
      const timer = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(timer);
    }
  }, [user, navigate]);

  if (!user) return null;

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-lg">
          <ScrollReveal>
            <h1 className="text-2xl font-bold text-foreground sm:text-3xl">
              Get credits
            </h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Current balance:{" "}
              <span className="font-semibold text-foreground">
                {user.tokens.toLocaleString()} credits
              </span>
            </p>
          </ScrollReveal>

          <ScrollReveal>
            <div className="mt-10 rounded-xl border border-border bg-card p-6 space-y-4">
              <p className="text-sm font-bold text-foreground">
                Credits &mdash; coming soon
              </p>
              <p className="text-sm text-muted-foreground leading-relaxed">
                Automated credit purchasing is not yet available. To top up your
                account, please contact us and we will add credits manually.
              </p>
              <Link
                to="/contact"
                className="inline-flex items-center gap-2 rounded-lg bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground shadow-md transition-all hover:opacity-90 hover:shadow-lg"
              >
                <Mail size={14} />
                Contact us for credits
              </Link>
            </div>
          </ScrollReveal>
        </div>
      </section>

      <Footer />
    </div>
  );
}
