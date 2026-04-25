/**
 * BrandMark — the small "D + phosphor dot" wordmark used on auth, error,
 * and chrome-light pages.  One unit, never decomposed.
 */
import { Link } from "react-router-dom";
import { BRAND } from "@/lib/brand";
import { cn } from "@/lib/utils";

export function BrandMark({
  className,
  size = "md",
  asLink = true,
}: {
  className?: string;
  size?: "sm" | "md" | "lg";
  asLink?: boolean;
}) {
  const sz = size === "lg" ? "h-9 w-9 text-[18px]" : size === "sm" ? "h-6 w-6 text-[12px]" : "h-8 w-8 text-[15px]";
  const wordSz = size === "lg" ? "text-xl" : size === "sm" ? "text-sm" : "text-[17px]";
  const inner = (
    <span className={cn("group inline-flex items-center gap-2", className)} aria-label={`${BRAND.name} home`}>
      <span
        className={cn(
          "relative inline-flex items-center justify-center rounded-md border border-border/70 bg-surface-1 font-mono font-semibold text-foreground transition-all group-hover:border-accent/60",
          sz,
        )}
        aria-hidden
      >
        D
        <span
          className="absolute -right-0.5 -top-0.5 size-1.5 rounded-full bg-deploy"
          style={{ boxShadow: "0 0 6px hsl(var(--deploy) / 0.7)" }}
        />
      </span>
      <span className={cn("font-semibold tracking-tight text-foreground", wordSz)}>{BRAND.name}</span>
    </span>
  );
  return asLink ? <Link to="/">{inner}</Link> : inner;
}
