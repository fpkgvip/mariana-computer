/**
 * Hook for live credit balance.
 *
 * B-08: Navbar/BuyCredits were rendering user.tokens from AuthContext, which
 * is set once on session-sync and never refreshed after a spend or webhook.
 * This hook is the single source of truth for the displayed balance.
 *
 * Refresh strategy (defence-in-depth):
 *   1. Fetch on mount (and whenever `user` identity changes).
 *   2. Subscribe to the custom DOM event "deft:credits-changed" — any part of
 *      the app can fire this to force an immediate refetch.
 *   3. Refresh on window "focus" — catches the tab-switch-after-spend case.
 *   4. Refresh on document "visibilitychange" to visible — same rationale for
 *      mobile backgrounding.
 *   5. Poll every `pollMs` ms as a backstop (default 30 s). Keeps the balance
 *      fresh in long-lived sessions without relying solely on events.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBalance, type BalanceResponse } from "@/lib/agentApi";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";

export const CREDITS_CHANGED_EVENT = "deft:credits-changed";

/** Default backstop poll interval (ms). 30 s is low-cost but keeps the
 *  balance fresh in long-lived sessions. Pass 0 to disable. */
const DEFAULT_POLL_MS = 30_000;

export interface UseCreditsState {
  balance: number;
  nextExpiry: string | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

export function useCredits(pollMs = DEFAULT_POLL_MS): UseCreditsState {
  const { user } = useAuth();
  const [data, setData] = useState<BalanceResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const refetch = useCallback(() => {
    if (!user) return;
    const controller = new AbortController();
    abortRef.current?.abort();
    abortRef.current = controller;
    setLoading(true);
    fetchBalance(controller.signal)
      .then((resp) => {
        setData(resp);
        setError(null);
      })
      .catch((err: unknown) => {
        if ((err as Error).name === "AbortError") return;
        if (err instanceof ApiError) setError(err.message);
        else setError("Could not fetch balance.");
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  }, [user]);

  // 1. Fetch on mount / user identity change.
  useEffect(() => {
    refetch();
    return () => abortRef.current?.abort();
  }, [refetch]);

  // 2. Custom DOM event — any spend/grant path fires this to force a refetch.
  useEffect(() => {
    const handler = () => refetch();
    window.addEventListener(CREDITS_CHANGED_EVENT, handler);
    return () => window.removeEventListener(CREDITS_CHANGED_EVENT, handler);
  }, [refetch]);

  // 3. Window focus — catches the tab-switch-after-spend case.
  useEffect(() => {
    const handler = () => refetch();
    window.addEventListener("focus", handler);
    return () => window.removeEventListener("focus", handler);
  }, [refetch]);

  // 4. visibilitychange to visible — covers mobile backgrounding.
  useEffect(() => {
    const handler = () => {
      if (document.visibilityState === "visible") refetch();
    };
    document.addEventListener("visibilitychange", handler);
    return () => document.removeEventListener("visibilitychange", handler);
  }, [refetch]);

  // 5. Backstop poll.
  useEffect(() => {
    if (pollMs <= 0) return;
    const id = window.setInterval(() => refetch(), pollMs);
    return () => window.clearInterval(id);
  }, [pollMs, refetch]);

  return {
    balance: data?.balance ?? 0,
    nextExpiry: data?.next_expiry ?? null,
    loading,
    error,
    refetch,
  };
}
