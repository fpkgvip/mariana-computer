import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/AuthContext";
import { toast } from "sonner";

const presets = [10, 25, 50, 100];

export default function BuyCredits() {
  const { user, buyCredits } = useAuth();
  const navigate = useNavigate();
  const [amount, setAmount] = useState(25);
  const [custom, setCustom] = useState("");
  const [useCustom, setUseCustom] = useState(false);
  const [processing, setProcessing] = useState(false);

  // Card form (visual only)
  const [card, setCard] = useState({ number: "", expiry: "", cvc: "" });

  useEffect(() => {
    if (!user) navigate("/login", { replace: true });
  }, [user, navigate]);

  const finalAmount = useCustom ? Number(custom) || 0 : amount;

  const handlePurchase = (e: React.FormEvent) => {
    e.preventDefault();
    if (finalAmount < 1) {
      toast.error("Minimum purchase is $1");
      return;
    }
    setProcessing(true);
    setTimeout(() => {
      buyCredits(finalAmount);
      setProcessing(false);
      toast.success(`$${finalAmount} in credits added`, {
        description: `${finalAmount * 10} tokens added to your account.`,
      });
      setCard({ number: "", expiry: "", cvc: "" });
    }, 1200);
  };

  if (!user) return null;

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-lg">
          <ScrollReveal>
            <h1 className="font-serif text-2xl font-semibold text-foreground sm:text-3xl">
              Buy credits
            </h1>
            <p className="mt-2 text-sm text-muted-foreground">
              Current balance: <span className="font-medium text-foreground">${(user.tokens / 10).toFixed(2)}</span>
            </p>
          </ScrollReveal>

          <form onSubmit={handlePurchase} className="mt-10 space-y-8">
            {/* Amount selector */}
            <ScrollReveal>
              <div>
                <label className="mb-3 block text-xs font-medium uppercase tracking-wider text-muted-foreground">
                  Amount
                </label>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {presets.map((p) => (
                    <button
                      key={p}
                      type="button"
                      onClick={() => { setAmount(p); setUseCustom(false); }}
                      className={`rounded-md border px-4 py-3 text-sm font-medium transition-colors ${
                        !useCustom && amount === p
                          ? "border-primary bg-primary/5 text-primary"
                          : "border-border text-muted-foreground hover:border-foreground/20 hover:text-foreground"
                      }`}
                    >
                      ${p}
                    </button>
                  ))}
                </div>
                <div className="mt-3 flex items-center gap-3">
                  <button
                    type="button"
                    onClick={() => setUseCustom(true)}
                    className={`text-xs font-medium transition-colors ${useCustom ? "text-primary" : "text-muted-foreground hover:text-foreground"}`}
                  >
                    Custom amount
                  </button>
                  {useCustom && (
                    <Input
                      type="number"
                      min={1}
                      value={custom}
                      onChange={(e) => setCustom(e.target.value)}
                      placeholder="Enter amount"
                      className="w-32"
                    />
                  )}
                </div>
              </div>
            </ScrollReveal>

            {/* Card details (fake) */}
            <ScrollReveal>
              <div className="space-y-4">
                <label className="mb-1 block text-xs font-medium uppercase tracking-wider text-muted-foreground">
                  Payment details
                </label>
                <Input
                  value={card.number}
                  onChange={(e) => setCard({ ...card, number: e.target.value })}
                  placeholder="4242 4242 4242 4242"
                  maxLength={19}
                  required
                />
                <div className="grid grid-cols-2 gap-3">
                  <Input
                    value={card.expiry}
                    onChange={(e) => setCard({ ...card, expiry: e.target.value })}
                    placeholder="MM / YY"
                    maxLength={7}
                    required
                  />
                  <Input
                    value={card.cvc}
                    onChange={(e) => setCard({ ...card, cvc: e.target.value })}
                    placeholder="CVC"
                    maxLength={4}
                    required
                  />
                </div>
              </div>
            </ScrollReveal>

            {/* Summary */}
            <ScrollReveal>
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">Credits</span>
                  <span className="font-medium text-foreground">{finalAmount * 10} tokens</span>
                </div>
                <div className="mt-2 flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">Total</span>
                  <span className="font-semibold text-foreground">${finalAmount.toFixed(2)}</span>
                </div>
              </div>

              <Button type="submit" disabled={processing || finalAmount < 1} className="mt-4 w-full">
                {processing ? "Processing…" : `Purchase $${finalAmount.toFixed(2)}`}
              </Button>
            </ScrollReveal>
          </form>
        </div>
      </section>

      <Footer />
    </div>
  );
}
