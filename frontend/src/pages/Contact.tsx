import { useState } from "react";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Mail, MapPin } from "lucide-react";
import { BRAND } from "@/lib/brand";

const subjects = ["General inquiry", "Sales", "Support", "Legal", "Partnership"];

// BUG-025: Removed placeholder 555 phone number — not a real contact number.
const contactInfo = [
  { icon: Mail, label: "Sales", value: BRAND.saleEmail },
  { icon: Mail, label: "Support", value: BRAND.supportEmail },
  { icon: Mail, label: "Legal", value: BRAND.legalEmail },
  { icon: MapPin, label: "Office", value: "140 Broadway, 46th Floor\nNew York, NY 10005" },
];

const API_URL = import.meta.env.VITE_API_URL ?? "";

export default function Contact() {
  const [form, setForm] = useState({ name: "", email: "", subject: subjects[0], message: "" });
  const [sending, setSending] = useState(false);

  const update = (field: string, value: string) => setForm((f) => ({ ...f, [field]: value }));

  // BUG-011: Actually send the form data to the backend instead of a setTimeout stub.
  // Falls back to a mailto link if the API endpoint is unavailable.
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSending(true);
    try {
      const res = await fetch(`${API_URL}/api/contact`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      toast.success("Message sent", {
        description: "We'll get back to you within 1 business day.",
      });
      setForm({ name: "", email: "", subject: subjects[0], message: "" });
    } catch {
      // If the backend contact endpoint isn't available, instruct the user to email directly
      toast.error("Could not send message", {
        description: `Please email us directly at ${BRAND.supportEmail}`,
      });
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-5xl">
          <ScrollReveal>
            <h1 className="font-serif text-3xl font-semibold text-foreground sm:text-4xl md:text-5xl">
              Talk to a human.
            </h1>
            <p className="mt-4 max-w-xl text-base text-muted-foreground md:text-lg">
              Sales, support, legal, partnerships. One business day, every email.
              For account questions you can also reach us from inside the app.
            </p>
          </ScrollReveal>

          <div className="mt-12 grid gap-12 md:mt-16 lg:grid-cols-2 lg:gap-20">
            {/* Contact info */}
            <ScrollReveal>
              <div className="space-y-6">
                {contactInfo.map((item) => (
                  <div key={item.label} className="flex gap-4">
                    <item.icon size={18} className="mt-0.5 shrink-0 text-muted-foreground" />
                    <div>
                      <p className="text-xs font-medium uppercase tracking-wider text-muted-foreground">
                        {item.label}
                      </p>
                      <p className="mt-1 whitespace-pre-line text-sm text-foreground">{item.value}</p>
                    </div>
                  </div>
                ))}
              </div>
            </ScrollReveal>

            {/* Form */}
            <ScrollReveal>
              <form onSubmit={handleSubmit} className="space-y-5">
                <div className="grid gap-5 sm:grid-cols-2">
                  <div>
                    <label htmlFor="contact-name" className="mb-1.5 block text-xs font-medium text-muted-foreground">Name</label>
                    <Input id="contact-name" value={form.name} onChange={(e) => update("name", e.target.value)} required placeholder="Your name" />
                  </div>
                  <div>
                    <label htmlFor="contact-email" className="mb-1.5 block text-xs font-medium text-muted-foreground">Email</label>
                    <Input id="contact-email" type="email" value={form.email} onChange={(e) => update("email", e.target.value)} required placeholder="you@firm.com" />
                  </div>
                </div>

                <div>
                  <label htmlFor="contact-subject" className="mb-1.5 block text-xs font-medium text-muted-foreground">Subject</label>
                  <select
                    id="contact-subject"
                    value={form.subject}
                    onChange={(e) => update("subject", e.target.value)}
                    className="w-full rounded-md border border-input bg-background px-3 py-2.5 text-sm text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  >
                    {subjects.map((s) => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                </div>

                <div>
                  <label htmlFor="contact-message" className="mb-1.5 block text-xs font-medium text-muted-foreground">Message</label>
                  <textarea
                    id="contact-message"
                    value={form.message}
                    onChange={(e) => update("message", e.target.value)}
                    required
                    rows={5}
                    placeholder="How can we help?"
                    className="w-full rounded-md border border-input bg-background px-3 py-2.5 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
                  />
                </div>

                <Button type="submit" disabled={sending} className="w-full sm:w-auto">
                  {sending ? "Sending…" : "Send message"}
                </Button>
              </form>
            </ScrollReveal>
          </div>
        </div>
      </section>

      <Footer />
    </div>
  );
}
