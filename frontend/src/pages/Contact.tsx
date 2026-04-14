import { useState } from "react";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { toast } from "sonner";
import { Mail, Phone, MapPin } from "lucide-react";

const subjects = ["General inquiry", "Sales", "Support", "Legal", "Partnership"];

const contactInfo = [
  { icon: Phone, label: "Phone", value: "+1 (212) 555-0147" },
  { icon: Mail, label: "Sales", value: "sales@mariana.co" },
  { icon: Mail, label: "Support", value: "support@mariana.co" },
  { icon: Mail, label: "Legal", value: "legal@mariana.co" },
  { icon: MapPin, label: "Office", value: "140 Broadway, 46th Floor\nNew York, NY 10005" },
];

export default function Contact() {
  const [form, setForm] = useState({ name: "", email: "", subject: subjects[0], message: "" });
  const [sending, setSending] = useState(false);

  const update = (field: string, value: string) => setForm((f) => ({ ...f, [field]: value }));

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setSending(true);
    setTimeout(() => {
      setSending(false);
      toast.success("Message sent", { description: "We'll get back to you within 1 business day." });
      setForm({ name: "", email: "", subject: subjects[0], message: "" });
    }, 800);
  };

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-5xl">
          <ScrollReveal>
            <h1 className="font-serif text-3xl font-semibold text-foreground sm:text-4xl md:text-5xl">
              Get in touch
            </h1>
            <p className="mt-4 max-w-xl text-base text-muted-foreground md:text-lg">
              Whether you're exploring Mariana for your firm or need help with your account, we're here.
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
                    <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Name</label>
                    <Input value={form.name} onChange={(e) => update("name", e.target.value)} required placeholder="Your name" />
                  </div>
                  <div>
                    <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Email</label>
                    <Input type="email" value={form.email} onChange={(e) => update("email", e.target.value)} required placeholder="you@firm.com" />
                  </div>
                </div>

                <div>
                  <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Subject</label>
                  <select
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
                  <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Message</label>
                  <textarea
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
