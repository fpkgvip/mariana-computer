/**
 * P13 — Shared list-state primitives.
 *
 * Use ErrorState for any async failure surface, EmptyState for any "no rows"
 * surface, and LoadingRows / InlineLoading for any pending state. Adopting
 * these everywhere makes the product's voice consistent: calm copy, copyable
 * request id on errors, and a clear primary action on empties.
 */
export { ErrorState } from "./ErrorState";
export type { ErrorStateProps } from "./ErrorState";
export { EmptyState } from "./EmptyState";
export type { EmptyStateProps } from "./EmptyState";
export { LoadingRows, InlineLoading } from "./LoadingState";
export type { LoadingRowsProps } from "./LoadingState";

/**
 * Helper: turn an unknown caught value into a stable error display payload.
 * Plays well with ApiError (carries requestId) and falls back gracefully
 * for plain Error and non-Error throws.
 */
import { ApiError } from "@/lib/api";

export interface ErrorDisplay {
  message: string;
  requestId: string | null;
}

export function describeError(err: unknown): ErrorDisplay {
  if (err instanceof ApiError) {
    return { message: err.message, requestId: err.requestId };
  }
  if (err instanceof Error) {
    return { message: err.message, requestId: null };
  }
  return { message: String(err), requestId: null };
}
