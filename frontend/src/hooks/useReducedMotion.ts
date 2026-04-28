import { useEffect, useState } from "react";

/**
 * Returns `true` when the user has requested reduced motion via the OS-level
 * `prefers-reduced-motion: reduce` media query.
 *
 * The CSS layer in `index.css` already neutralises animation/transition
 * durations globally for users who opt out; this hook is the JS-side
 * counterpart for components that drive motion through `setTimeout`,
 * `IntersectionObserver`, or React state (e.g. typing-effect placeholders,
 * staggered reveals). Components should branch on the returned value to skip
 * the motion entirely rather than running it at compressed timing.
 */
export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState<boolean>(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return false;
    }
    return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  });

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = (e: MediaQueryListEvent) => setReduced(e.matches);
    // Older Safari uses `addListener`, modern browsers use `addEventListener`.
    if (typeof mq.addEventListener === "function") {
      mq.addEventListener("change", onChange);
      return () => mq.removeEventListener("change", onChange);
    }
    mq.addListener(onChange);
    return () => mq.removeListener(onChange);
  }, []);

  return reduced;
}
