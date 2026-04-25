/**
 * Deft API client — single entry point for backend calls.
 *
 * Design:
 *  - Always uses relative URLs in production (Vercel rewrites /api/* to backend).
 *  - VITE_API_URL is honored only for local dev pointing at a remote backend.
 *  - Always attaches Supabase access token from current session.
 *  - Returns typed JSON; throws ApiError with structured details on non-2xx.
 *  - All calls are AbortSignal-aware.
 */

import { supabase } from "@/lib/supabase";
import { addBreadcrumb } from "@/lib/observability";

const API_BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");

export interface ApiErrorBody {
  detail?: unknown;
  message?: string;
  type?: string;
}

export class ApiError extends Error {
  status: number;
  body: ApiErrorBody | null;
  /**
   * Backend request id, when the server returned X-Request-Id. Carried so that
   * UI surfaces (toasts, ErrorState, report-issue) can echo a copyable
   * identifier for support without forcing the user to dig through devtools.
   */
  requestId: string | null;
  constructor(
    status: number,
    message: string,
    body: ApiErrorBody | null,
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
    this.requestId = requestId;
  }
}

async function getAccessToken(): Promise<string | null> {
  try {
    const { data } = await supabase.auth.getSession();
    return data.session?.access_token ?? null;
  } catch {
    return null;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** If true, do NOT attach the Supabase token. */
  anonymous?: boolean;
}

export async function apiRequest<T = unknown>(
  path: string,
  opts: RequestOptions = {},
): Promise<T> {
  const url = `${API_BASE}${path.startsWith("/") ? "" : "/"}${path}`;
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(opts.headers ?? {}),
  };

  if (opts.body !== undefined && !(opts.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }

  if (!opts.anonymous) {
    const token = await getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  const init: RequestInit = {
    method: opts.method ?? "GET",
    headers,
    signal: opts.signal,
  };
  if (opts.body !== undefined) {
    init.body =
      opts.body instanceof FormData
        ? opts.body
        : JSON.stringify(opts.body);
  }

  let resp: Response;
  try {
    resp = await fetch(url, init);
  } catch (err) {
    // Network / abort. Re-throw aborts as-is so callers can detect.
    if ((err as Error).name === "AbortError") throw err;
    addBreadcrumb({
      category: "api",
      level: "error",
      message: `network error ${init.method} ${path}`,
    });
    throw new ApiError(0, "Network error", null);
  }

  const contentType = resp.headers.get("content-type") ?? "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await resp.json().catch(() => null) : await resp.text();

  if (!resp.ok) {
    const body = (isJson ? payload : null) as ApiErrorBody | null;
    const detail = body?.detail;
    const detailMsg =
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail.map((d) => (typeof d === "object" && d ? (d as { msg?: string }).msg ?? "validation error" : String(d))).join(", ")
          : body?.message ?? `Request failed (HTTP ${resp.status})`;
    const requestId = resp.headers.get("x-request-id");
    addBreadcrumb({
      category: "api",
      level: resp.status >= 500 ? "error" : "warning",
      message: `${init.method} ${path} -> ${resp.status}`,
      data: { request_id: requestId, status: resp.status },
    });
    throw new ApiError(resp.status, String(detailMsg), body, requestId);
  }
  addBreadcrumb({
    category: "api",
    message: `${init.method} ${path} -> ${resp.status}`,
    data: { request_id: resp.headers.get("x-request-id") },
  });
  return payload as T;
}

export const api = {
  get: <T,>(path: string, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...opts, method: "GET" }),
  post: <T,>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...opts, method: "POST", body }),
  put: <T,>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...opts, method: "PUT", body }),
  patch: <T,>(path: string, body?: unknown, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...opts, method: "PATCH", body }),
  delete: <T,>(path: string, opts?: Omit<RequestOptions, "method" | "body">) =>
    apiRequest<T>(path, { ...opts, method: "DELETE" }),
};
