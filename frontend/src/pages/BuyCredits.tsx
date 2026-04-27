import { useEffect } from "react";
import { useNavigate, Link } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { useAuth } from "@/contexts/AuthContext";
// B-08: useCredits provides live balance that refreshes after spend/webhook.
// Replaces stale user.tokens from AuthContext which was set once on session sync.
import { useCredits } from "@/hooks/useCredits";
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
  // B-08: live balance — auto-refreshes on focus, visibilitychange, deft:credits-changed, and poll.
  const { balance: liveBalance } = useCredits();
  const navigate = useNavigate();

  // BUG-R1-10: Add a 500ms grace period before redirecting, matching Chat.tsx.
  // Supabase token refresh briefly sets user=null; without the delay, users
  // navigating to this page during a refresh cycle are incorrectly sent to /login.
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
            <h1 className="font-serif text-2xl font-semibold text-foreground sm:text-3xl">
              Get credits
            </h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Current balance:{" "}
              <span className="font-medium text-foreground">
                {liveBalance.toLocaleString()} credits
              </span>
            </p>
          </ScrollReveal>

          <ScrollReveal>
            <div className="mt-10 rounded-lg border border-border bg-card p-6 space-y-4">
              <p className="text-sm text-foreground font-medium">
                Credits &mdash; coming soon
              </p>
              <p className="text-sm text-muted-foreground leading-relaxed">
                Automated credit purchasing is not yet available. To top up your
                account, please contact us and we will add credits manually.
              </p>
              <Link
                to="/contact"
                className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
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
