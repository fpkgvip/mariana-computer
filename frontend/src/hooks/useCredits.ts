/**
 * Hook for live credit balance.
 *
 * - Fetches /api/credits/balance on mount and every `pollMs` ms (default off).
 * - Subscribes to a custom DOM event "deft:credits-changed" so other parts of
 *   the app can request a refetch (e.g. after spend/grant).
 * - Exposes refetch() for explicit refresh.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchBalance, type BalanceResponse } from "@/lib/agentApi";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/contexts/AuthContext";

export const CREDITS_CHANGED_EVENT = "deft:credits-changed";

export interface UseCreditsState {
  balance: number;
  nextExpiry: string | null;
  loading: boolean;
  error: string | null;
  refetch: () => void;
}

export function useCredits(pollMs = 0): UseCreditsState {
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

  useEffect(() => {
    refetch();
    return () => abortRef.current?.abort();
  }, [refetch]);

  useEffect(() => {
    const handler = () => refetch();
    window.addEventListener(CREDITS_CHANGED_EVENT, handler);
    return () => window.removeEventListener(CREDITS_CHANGED_EVENT, handler);
  }, [refetch]);

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
