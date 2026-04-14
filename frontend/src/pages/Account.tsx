import { useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Button } from "@/components/ui/button";
import { useAuth } from "@/contexts/AuthContext";
import { LogOut, CreditCard, MessageSquare } from "lucide-react";

export default function Account() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  useEffect(() => {
    if (!user) navigate("/login", { replace: true });
  }, [user, navigate]);

  if (!user) return null;

  const handleLogout = () => {
    logout();
    navigate("/");
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-lg">
          <ScrollReveal>
            <h1 className="font-serif text-2xl font-semibold text-foreground sm:text-3xl">Account</h1>
          </ScrollReveal>

          <ScrollReveal>
            <div className="mt-8 rounded-lg border border-border bg-card p-6">
              <div className="space-y-4">
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Name</p>
                  <p className="mt-1 text-sm text-foreground">{user.name}</p>
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Email</p>
                  <p className="mt-1 text-sm text-foreground">{user.email}</p>
                </div>
                <div>
                  <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">Credit balance</p>
                  <p className="mt-1 text-lg font-semibold text-foreground">
                    ${(user.tokens / 10).toFixed(2)}
                    <span className="ml-2 text-xs font-normal text-muted-foreground">({user.tokens} tokens)</span>
                  </p>
                </div>
              </div>
            </div>
          </ScrollReveal>

          <ScrollReveal>
            <div className="mt-6 grid gap-3 sm:grid-cols-2">
              <Link to="/buy-credits">
                <Button variant="outline" className="w-full justify-start gap-2">
                  <CreditCard size={16} /> Buy credits
                </Button>
              </Link>
              <Link to="/chat">
                <Button variant="outline" className="w-full justify-start gap-2">
                  <MessageSquare size={16} /> Mariana Computer
                </Button>
              </Link>
            </div>

            <Button variant="ghost" onClick={handleLogout} className="mt-6 w-full justify-start gap-2 text-muted-foreground">
              <LogOut size={16} /> Sign out
            </Button>
          </ScrollReveal>
        </div>
      </section>

      <Footer />
    </div>
  );
}
