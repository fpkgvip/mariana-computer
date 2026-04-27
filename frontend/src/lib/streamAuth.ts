/**
 * B-09: Shared stream-token helpers for SSE authentication.
 *
 * The EventSource API does not support custom request headers, which means
 * SSE URLs must carry some form of credential in the query string.  Placing
 * the full Supabase JWT there exposes it in nginx/CDN access logs, browser
 * history, and Referer headers.
 *
 * This module mints short-lived opaque stream tokens from the backend
 * (valid for 2 minutes, HMAC-signed, task-scoped) and provides helpers
 * that both Chat.tsx and AgentTaskView.tsx call so the logic lives in
 * exactly one place.
 *
 * TODO B-09-FOLLOWUP: Replace EventSource with fetch() + ReadableStream so
 * the token can be sent in an Authorization header instead of a query param.
 */

export interface StreamTokenResult {
  stream_token: string;
  expires_in_seconds: number;
}

/**
 * Mint a short-lived stream token for an investigation SSE endpoint.
 *
 * Calls `POST /api/investigations/{taskId}/stream-token` with the user's
 * normal Bearer JWT.  Returns the opaque token that the SSE URL accepts.
 *
 * Throws an error — never falls back to the raw JWT — so callers can surface
 * a user-visible error rather than silently leaking the JWT in the URL.
 */
export async function mintInvestigationStreamToken(
  apiUrl: string,
  taskId: string,
  bearerJwt: string,
): Promise<string> {
  const res = await fetch(
    `${apiUrl}/api/investigations/${encodeURIComponent(taskId)}/stream-token`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${bearerJwt}` },
    },
  );
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(
      `stream-token mint failed (${res.status}): ${body || res.statusText}`,
    );
  }
  const data: StreamTokenResult = await res.json();
  if (!data.stream_token) {
    throw new Error("stream-token response missing stream_token field");
  }
  return data.stream_token;
}

/**
 * Mint a short-lived stream token for an agent task SSE endpoint.
 *
 * Calls `POST /api/agent/{taskId}/stream-token`.  Same contract as
 * mintInvestigationStreamToken — throws on any failure.
 */
export async function mintAgentStreamToken(
  apiUrl: string,
  taskId: string,
  bearerJwt: string,
): Promise<string> {
  const res = await fetch(
    `${apiUrl}/api/agent/${encodeURIComponent(taskId)}/stream-token`,
    {
      method: "POST",
      headers: { Authorization: `Bearer ${bearerJwt}` },
    },
  );
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(
      `agent stream-token mint failed (${res.status}): ${body || res.statusText}`,
    );
  }
  const data: StreamTokenResult = await res.json();
  if (!data.stream_token) {
    throw new Error("agent stream-token response missing stream_token field");
  }
  return data.stream_token;
}

/**
 * Assert that a URL does not contain a raw JWT.
 *
 * JWTs always start with "eyJ" (base64url of `{"alg":...}`).  If the URL
 * contains that prefix, it almost certainly carries a raw JWT in a query
 * param — which is the exact vulnerability B-09 fixes.  Useful in tests.
 */
export function assertNoRawJwt(url: string): void {
  if (url.includes("eyJ")) {
    throw new Error(
      `B-09: SSE URL contains a raw JWT (eyJ...) — use a stream token instead. URL: ${url}`,
    );
  }
}
