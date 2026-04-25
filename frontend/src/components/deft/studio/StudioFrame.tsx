/**
 * StudioFrame — the studio chrome: left rail (projects) + main area.
 *
 * Owns:
 *  - Sticky three-zone layout (projects · main) under a fixed Navbar
 *  - Mobile slide-over for the projects sidebar (<lg:1024)
 *  - Calm header bar with project name, stage chip, credits, cancel
 *
 * Composition:
 *  <StudioFrame projects={...} header={...}>
 *    <YourMainContent />
 *  </StudioFrame>
 *
 * No data-fetching here — pure layout. Children own state.
 */
import { ReactNode, useEffect, useState } from "react";
import { Menu, X } from "lucide-react";
import { cn } from "@/lib/utils";

interface StudioFrameProps {
  /** The projects rail. Fixed width on lg+, slide-over below. */
  projects: ReactNode;
  /** Optional sticky header strip rendered above the main content. */
  header?: ReactNode;
  /** Main content (IdleStudio or LiveStudio). */
  children: ReactNode;
  /** Disable the slide-over (e.g. in dev mock mode without a real sidebar). */
  hideMobileToggle?: boolean;
}

export function StudioFrame({ projects, header, children, hideMobileToggle }: StudioFrameProps) {
  const [drawerOpen, setDrawerOpen] = useState(false);

  // Close drawer on Escape and on resize up to lg
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDrawerOpen(false);
    };
    const onResize = () => {
      if (window.innerWidth >= 1024) setDrawerOpen(false);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onResize);
    };
  }, []);

  // Lock body scroll when drawer is open
  useEffect(() => {
    if (drawerOpen) {
      const prev = document.body.style.overflow;
      document.body.style.overflow = "hidden";
      return () => {
        document.body.style.overflow = prev;
      };
    }
  }, [drawerOpen]);

  return (
    <div className="flex flex-1 min-h-0 overflow-hidden">
      {/* Persistent rail on lg+ */}
      <div className="hidden lg:flex">{projects}</div>

      {/* Slide-over drawer below lg */}
      {!hideMobileToggle && drawerOpen && (
        <div className="fixed inset-0 z-40 lg:hidden" role="dialog" aria-modal="true" aria-label="Projects">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => setDrawerOpen(false)}
            aria-hidden
          />
          <div className="relative h-full w-[min(280px,86vw)] border-r border-border bg-[hsl(var(--sidebar-background))] shadow-elev-2">
            <button
              type="button"
              onClick={() => setDrawerOpen(false)}
              className="absolute right-2 top-2 inline-flex h-8 w-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
              aria-label="Close projects"
            >
              <X size={14} />
            </button>
            {projects}
          </div>
        </div>
      )}

      <main className="relative flex flex-1 min-h-0 flex-col overflow-hidden">
        {header && (
          <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-border/70 bg-[hsl(var(--bg-0)/0.85)] px-3 py-2 backdrop-blur">
            {!hideMobileToggle && (
              <button
                type="button"
                onClick={() => setDrawerOpen(true)}
                aria-label="Open projects"
                className={cn(
                  "inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md",
                  "text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground",
                  "lg:hidden",
                )}
              >
                <Menu size={15} />
              </button>
            )}
            <div className="min-w-0 flex-1">{header}</div>
          </div>
        )}
        <div className="relative flex-1 min-h-0 overflow-hidden">{children}</div>
      </main>
    </div>
  );
}
