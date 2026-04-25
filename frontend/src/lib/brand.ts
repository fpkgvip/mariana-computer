/**
 * Deft brand constants.
 * Single source of truth for user-visible brand strings.
 *
 * When updating brand language, change it HERE. Importers must reference
 * BRAND.* — never hard-code "Deft" in components.
 */

export const BRAND = {
  // Primary identity
  name: 'Deft',
  fullName: 'Deft',
  product: 'Deft',
  tagline: 'One prompt to a deployed app.',
  shortTagline: 'Prompt. Build. Ship.',
  description:
    'Deft turns a single prompt into a deployed web app. Plan, write, build, verify, ship — no debug hell, no setup, no copy-paste. Generation is free; pay only when you deploy.',
  ogDescription:
    'One prompt. A deployed app. Deft replaces every vibe-coding tool in one autonomous engineer.',

  // Domains & contact
  domain: 'deft.computer',
  url: 'https://deft.computer',
  appUrl: 'https://app.deft.computer',
  emailDomain: 'deft.computer',
  supportEmail: 'support@deft.computer',
  saleEmail: 'sale@deft.computer',
  legalEmail: 'legal@deft.computer',

  // Social handles (placeholder until claimed)
  twitter: '@deftcomputer',
  github: 'deft-computer',

  // Copyright / legal
  legalName: 'Deft Computer, Inc.',
  copyrightYear: 2026,

  // Voice descriptors (used in onboarding copy guidance, NOT printed)
  voice: {
    tone: 'calm operator',
    promise: 'One prompt. A deployed app. No debug hell.',
  },
} as const;

export type Brand = typeof BRAND;

/** DOM event names — kept here so we never have a stray `mariana:*` event. */
export const EVENTS = {
  logout: 'deft:logout',
  authChanged: 'deft:auth-changed',
  taskUpdated: 'deft:task-updated',
} as const;

/** localStorage key prefixes — namespaced under brand. */
export const STORAGE = {
  recentPrompts: 'deft.recent_prompts',
  uiPrefs: 'deft.ui_prefs',
  onboardingState: 'deft.onboarding',
} as const;
