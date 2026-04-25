/**
 * Lightweight analytics wrapper for the 8 Phase-6 onboarding/usage events.
 *
 * - PostHog is initialised lazily once at app boot via initAnalytics().
 * - When VITE_POSTHOG_KEY is unset (dev mode), every track() call falls back
 *   to console.debug so we still see what would have been emitted.
 * - The 8 canonical event names are exported as a const tuple so callsites
 *   can't fat-finger an event name.
 */
import posthog from "posthog-js";

let initialised = false;
let enabled = false;

const POSTHOG_HOST = "https://us.i.posthog.com";

export const ANALYTICS_EVENTS = [
  "signup_completed",
  "onboarding_step_viewed",
  "onboarding_completed",
  "onboarding_skipped",
  "first_prompt_submitted",
  "quote_generated",
  "checkout_started",
  "vault_secret_added",
] as const;

export type AnalyticsEvent = (typeof ANALYTICS_EVENTS)[number];

export function initAnalytics(): void {
  if (initialised) return;
  initialised = true;
  const key = import.meta.env.VITE_POSTHOG_KEY as string | undefined;
  if (!key) {
    // Dev / preview without PostHog — quietly disable, no errors.
    return;
  }
  try {
    posthog.init(key, {
      api_host: (import.meta.env.VITE_POSTHOG_HOST as string | undefined) ?? POSTHOG_HOST,
      capture_pageview: true,
      autocapture: false, // stay GDPR-friendly; only track explicit events
      disable_session_recording: true,
      persistence: "localStorage+cookie",
    });
    enabled = true;
  } catch (err) {
    // Never let analytics break the app.
    // eslint-disable-next-line no-console
    console.warn("[analytics] posthog init failed:", err);
  }
}

export function identifyUser(userId: string, traits?: Record<string, unknown>): void {
  if (!enabled) {
    // eslint-disable-next-line no-console
    console.debug("[analytics:dev] identify", userId, traits);
    return;
  }
  try {
    posthog.identify(userId, traits);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("[analytics] identify failed:", err);
  }
}

export function track(
  event: AnalyticsEvent,
  properties?: Record<string, unknown>,
): void {
  if (!enabled) {
    // eslint-disable-next-line no-console
    console.debug(`[analytics:dev] ${event}`, properties ?? {});
    return;
  }
  try {
    posthog.capture(event, properties);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(`[analytics] capture(${event}) failed:`, err);
  }
}

export function resetAnalytics(): void {
  if (!enabled) return;
  try {
    posthog.reset();
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn("[analytics] reset failed:", err);
  }
}
