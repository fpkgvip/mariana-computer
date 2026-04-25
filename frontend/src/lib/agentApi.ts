/**
 * Deft agent + credits API surface.
 * Thin typed wrappers around the FastAPI billing routes.
 */
import { api } from "@/lib/api";

export type ModelTier = "lite" | "standard" | "max";

export interface QuoteRequest {
  prompt: string;
  tier?: ModelTier;
  max_credits?: number | null;
}

export interface QuoteBreakdown {
  tier_baseline_credits: number;
  tier_variance: number;
  complexity_score: number;
  ceiling_applied: number | null;
}

export interface QuoteResponse {
  tier: ModelTier;
  credits_min: number;
  credits_max: number;
  eta_seconds_min: number;
  eta_seconds_max: number;
  complexity_score: number;
  breakdown: QuoteBreakdown;
}

export interface BalanceResponse {
  balance: number;
  next_expiry: string | null;
}

export const TIER_LABEL: Record<ModelTier, string> = {
  lite: "Lite",
  standard: "Standard",
  max: "Max",
};

export const TIER_DESCRIPTION: Record<ModelTier, string> = {
  lite: "Faster, cheaper. Best for small fixes.",
  standard: "Balanced default for most builds.",
  max: "Top reasoning. Best for hard or large tasks.",
};

export function fetchQuote(
  body: QuoteRequest,
  signal?: AbortSignal,
): Promise<QuoteResponse> {
  return api.post<QuoteResponse>("/api/agent/quote", body, { signal });
}

export function fetchBalance(signal?: AbortSignal): Promise<BalanceResponse> {
  return api.get<BalanceResponse>("/api/credits/balance", { signal });
}

export function formatCreditsRange(min: number, max: number): string {
  if (min === max) return `${min.toLocaleString()} credits`;
  return `${min.toLocaleString()}–${max.toLocaleString()} credits`;
}

export function formatDollarsRange(min: number, max: number): string {
  // 1 credit == $0.01
  const lo = (min / 100).toFixed(2);
  const hi = (max / 100).toFixed(2);
  if (min === max) return `$${lo}`;
  return `$${lo}–$${hi}`;
}

export function formatEtaRange(minSec: number, maxSec: number): string {
  const fmt = (s: number) => {
    if (s < 60) return `${s}s`;
    if (s < 3600) {
      const m = Math.round(s / 60);
      return `${m} min`;
    }
    const h = Math.floor(s / 3600);
    const m = Math.round((s % 3600) / 60);
    return m === 0 ? `${h}h` : `${h}h ${m}m`;
  };
  if (minSec === maxSec) return fmt(minSec);
  // If both round to same minute display, just one
  const a = fmt(minSec);
  const b = fmt(maxSec);
  if (a === b) return a;
  return `${a}–${b}`;
}
