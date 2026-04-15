import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { ScrollReveal } from "@/components/ScrollReveal";
import { useAuth } from "@/contexts/AuthContext";
import { supabase } from "@/lib/supabase";
import { toast } from "sonner";
import {
  Zap,
  Plus,
  FileText,
  BarChart3,
  Target,
  LineChart,
  Presentation,
  Table,
  X,
  Loader2,
} from "lucide-react";

const API_URL = import.meta.env.VITE_API_URL ?? "";

/* ------------------------------------------------------------------ */
/*  Types                                                             */
/* ------------------------------------------------------------------ */

interface Skill {
  id: string;
  name: string;
  description: string;
  trigger_keywords: string[];
  category: string;
}

/* ------------------------------------------------------------------ */
/*  Built-in skills (client-side mirror of backend BUILTIN_SKILLS)    */
/* ------------------------------------------------------------------ */

const BUILTIN_SKILLS: Skill[] = [
  {
    id: "research-report",
    name: "Research Report",
    description:
      "Generate comprehensive research reports with citations, data analysis, and actionable conclusions.",
    trigger_keywords: ["report", "research report", "analysis", "deep dive"],
    category: "built-in",
  },
  {
    id: "financial-analysis",
    name: "Financial Analysis",
    description:
      "Analyze financial statements, SEC filings, and market data to produce investment-grade analysis.",
    trigger_keywords: ["financial", "earnings", "valuation", "SEC filing"],
    category: "built-in",
  },
  {
    id: "competitive-analysis",
    name: "Competitive Analysis",
    description:
      "Map competitive landscapes, identify market positioning, and analyze competitive dynamics.",
    trigger_keywords: ["competitive", "competition", "market share", "landscape"],
    category: "built-in",
  },
  {
    id: "data-analysis",
    name: "Data Analysis",
    description:
      "Quantitative analysis with statistical methods, data visualization descriptions, and pattern identification.",
    trigger_keywords: ["data", "statistics", "quantitative", "correlation"],
    category: "built-in",
  },
  {
    id: "presentation-builder",
    name: "Presentation Builder",
    description:
      "Create structured slide presentations from research findings.",
    trigger_keywords: ["presentation", "slides", "pptx", "powerpoint", "deck"],
    category: "built-in",
  },
  {
    id: "excel-model",
    name: "Excel Model Builder",
    description:
      "Build financial models, DCF valuations, and data tables in Excel format.",
    trigger_keywords: ["excel", "model", "spreadsheet", "dcf", "valuation model"],
    category: "built-in",
  },
];

const CATEGORY_ICONS: Record<string, React.FC<{ size?: number; className?: string }>> = {
  "research-report": FileText,
  "financial-analysis": BarChart3,
  "competitive-analysis": Target,
  "data-analysis": LineChart,
  "presentation-builder": Presentation,
  "excel-model": Table,
};

/* ------------------------------------------------------------------ */
/*  Create Skill Modal                                                */
/* ------------------------------------------------------------------ */

function CreateSkillModal({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (skill: Skill) => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [keywords, setKeywords] = useState("");
  const [saving, setSaving] = useState(false);

  if (!open) return null;

  const handleCreate = async () => {
    if (!name.trim() || !description.trim()) {
      toast.error("Name and description are required");
      return;
    }
    setSaving(true);
    try {
      const { data: { session } } = await supabase.auth.getSession();
      const token = session?.access_token;
      if (!token) {
        toast.error("Not authenticated");
        return;
      }

      const res = await fetch(`${API_URL}/api/skills`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name,
          description,
          system_prompt: systemPrompt,
          trigger_keywords: keywords
            .split(",")
            .map((k) => k.trim())
            .filter(Boolean),
        }),
      });

      if (!res.ok) {
        const errText = await res.text().catch(() => res.statusText);
        throw new Error(errText);
      }

      const skill: Skill = await res.json();
      onCreated(skill);
      toast.success("Skill created");
      onClose();
      setName("");
      setDescription("");
      setSystemPrompt("");
      setKeywords("");
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      toast.error("Failed to create skill", { description: msg });
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="fixed inset-0 z-50 bg-black/50" onClick={onClose} />
      <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
        <div className="w-full max-w-lg rounded-lg border border-border bg-card p-6 shadow-xl">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-base font-semibold text-foreground">
              Create Custom Skill
            </h3>
            <button onClick={onClose} className="text-muted-foreground hover:text-foreground">
              <X size={16} />
            </button>
          </div>

          <div className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Name</label>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:border-primary focus:outline-none"
                placeholder="e.g. M&A Analysis"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">Description</label>
              <input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:border-primary focus:outline-none"
                placeholder="What does this skill do?"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">
                System Prompt <span className="text-muted-foreground/50">(optional)</span>
              </label>
              <textarea
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
                rows={3}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:border-primary focus:outline-none resize-none"
                placeholder="Instructions for the AI when this skill is active..."
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-muted-foreground mb-1">
                Trigger Keywords <span className="text-muted-foreground/50">(comma-separated)</span>
              </label>
              <input
                value={keywords}
                onChange={(e) => setKeywords(e.target.value)}
                className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/50 focus:border-primary focus:outline-none"
                placeholder="merger, acquisition, m&a, deal"
              />
            </div>
          </div>

          <div className="mt-6 flex justify-end gap-2">
            <button
              onClick={onClose}
              className="rounded-md border border-border px-3 py-1.5 text-xs text-foreground hover:bg-secondary transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleCreate}
              disabled={saving || !name.trim()}
              className="rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 transition-colors"
            >
              {saving ? <Loader2 size={12} className="animate-spin" /> : "Create Skill"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}

/* ------------------------------------------------------------------ */
/*  Skills Page                                                       */
/* ------------------------------------------------------------------ */

export default function Skills() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [customSkills, setCustomSkills] = useState<Skill[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!user) {
      const timer = setTimeout(() => navigate("/login", { replace: true }), 500);
      return () => clearTimeout(timer);
    }
  }, [user, navigate]);

  useEffect(() => {
    if (!user) return;
    const loadCustomSkills = async () => {
      try {
        const { data: { session } } = await supabase.auth.getSession();
        const token = session?.access_token;
        if (!token) return;

        const res = await fetch(`${API_URL}/api/skills`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (res.ok) {
          const data: Skill[] = await res.json();
          setCustomSkills(data.filter((s) => s.category !== "built-in"));
        }
      } catch {
        // API may not have skills endpoint yet — silently ignore
      } finally {
        setLoading(false);
      }
    };
    loadCustomSkills();
  }, [user]);

  if (!user) return null;

  const allSkills = [...BUILTIN_SKILLS, ...customSkills];

  return (
    <div className="min-h-screen bg-background">
      <Navbar />

      <section className="px-6 pt-32 pb-16 md:pt-40 md:pb-24">
        <div className="mx-auto max-w-3xl">
          <ScrollReveal>
            <div className="flex items-center justify-between">
              <h1 className="font-serif text-2xl font-semibold text-foreground sm:text-3xl">
                Skills
              </h1>
              <button
                onClick={() => setShowCreate(true)}
                className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                <Plus size={12} />
                Create Custom Skill
              </button>
            </div>
            <p className="mt-2 text-sm text-muted-foreground">
              Skills are reusable instruction sets that guide how Mariana approaches different types
              of research. Skills are auto-detected based on your query, or you can select one
              manually.
            </p>
          </ScrollReveal>

          {loading ? (
            <div className="flex justify-center py-12">
              <Loader2 size={20} className="animate-spin text-muted-foreground" />
            </div>
          ) : (
            <div className="mt-8 grid gap-4 sm:grid-cols-2">
              {allSkills.map((skill, i) => {
                const Icon = CATEGORY_ICONS[skill.id] || Zap;
                return (
                  <ScrollReveal key={skill.id} delay={i * 60}>
                    <div className="rounded-lg border border-border bg-card p-5 shadow-sm">
                      <div className="flex items-start gap-3">
                        <div className="mt-0.5 rounded-md bg-primary/10 p-2">
                          <Icon size={16} className="text-primary" />
                        </div>
                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <h3 className="text-sm font-semibold text-foreground">{skill.name}</h3>
                            <span
                              className={`inline-flex rounded-full px-1.5 py-0.5 text-[9px] font-medium ring-1 ring-inset ${
                                skill.category === "built-in"
                                  ? "bg-accent/10 text-accent ring-accent/20"
                                  : "bg-blue-500/10 text-blue-400 ring-blue-500/20"
                              }`}
                            >
                              {skill.category}
                            </span>
                          </div>
                          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                            {skill.description}
                          </p>
                          <div className="mt-2 flex flex-wrap gap-1">
                            {skill.trigger_keywords.slice(0, 4).map((kw) => (
                              <span
                                key={kw}
                                className="rounded bg-secondary px-1.5 py-0.5 text-[9px] text-muted-foreground"
                              >
                                {kw}
                              </span>
                            ))}
                            {skill.trigger_keywords.length > 4 && (
                              <span className="text-[9px] text-muted-foreground/50">
                                +{skill.trigger_keywords.length - 4} more
                              </span>
                            )}
                          </div>
                        </div>
                      </div>
                    </div>
                  </ScrollReveal>
                );
              })}
            </div>
          )}
        </div>
      </section>

      <CreateSkillModal
        open={showCreate}
        onClose={() => setShowCreate(false)}
        onCreated={(skill) => setCustomSkills((prev) => [...prev, skill])}
      />

      <Footer />
    </div>
  );
}
