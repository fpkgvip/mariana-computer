import { useEffect, useRef, useState, ReactNode } from "react";
import { useReducedMotion } from "@/hooks/useReducedMotion";

interface ScrollRevealProps {
  children: ReactNode;
  className?: string;
  delay?: number;
}

// Cap any per-component delay so a section of stacked reveals can never
// take longer than ~150ms of cumulative stagger to settle. Audit §5.1
// flagged stacked paragraph-by-paragraph reveals as feeling slow on long
// marketing pages — clamping here fixes every caller in one place rather
// than touching dozens of `delay={i * 120}` call sites.
const MAX_DELAY_MS = 120;

export function ScrollReveal({ children, className = "", delay = 0 }: ScrollRevealProps) {
  const ref = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  // When reduced motion is requested, render content visible from the first
  // paint and skip the IntersectionObserver entirely. The CSS layer also
  // collapses transition durations, but skipping the observer avoids the
  // brief opacity-0 flash before the observer fires.
  const [visible, setVisible] = useState(reduced);
  const effectiveDelay = Math.max(0, Math.min(delay, MAX_DELAY_MS));

  useEffect(() => {
    if (reduced) {
      setVisible(true);
      return;
    }
    const el = ref.current;
    if (!el) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setTimeout(() => setVisible(true), effectiveDelay);
          observer.unobserve(el);
        }
      },
      { threshold: 0.1, rootMargin: "0px 0px -60px 0px" }
    );

    observer.observe(el);
    return () => observer.disconnect();
  }, [effectiveDelay, reduced]);

  return (
    <div
      ref={ref}
      className={`transition-all duration-500 ease-[cubic-bezier(0.16,1,0.3,1)] ${
        visible ? "opacity-100 translate-y-0" : "opacity-0 translate-y-6"
      } ${className}`}
    >
      {children}
    </div>
  );
}
