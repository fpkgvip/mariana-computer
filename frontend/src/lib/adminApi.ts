/**
 * Admin API client — wraps the v3.7 /api/admin/* endpoints.
 *
 * Every call forwards the Supabase access token as a Bearer header so the
 * backend can verify admin role via SECURITY DEFINER RPC functions.
 */
import { supabase } from "@/lib/supabase";

const API_URL = import.meta.env.VITE_API_URL ?? "";

export class AdminApiError extends Error {
  status: number;
  body: string;
  constructor(status: number, body: string, message?: string) {
    super(message ?? `Admin API error (${status}): ${body.slice(0, 200)}`);
    this.status = status;
    this.body = body;
  }
}

async function token(): Promise<string> {
  const { data } = await supabase.auth.getSession();
  const t = data.session?.access_token;
  if (!t) throw new AdminApiError(401, "", "Not authenticated");
  return t;
}

async function call<T>(
  method: "GET" | "POST" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  const tok = await token();
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${tok}`,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new AdminApiError(res.status, text);
  }
  // 204 No Content
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/* ---------- Types ---------------------------------------------------- */

export interface AdminOverview {
  total_users?: number;
  admins?: number;
  suspended?: number;
  active_24h?: number;
  active_30d?: number;
  tasks_total?: number;
  tasks_running?: number;
  tasks_24h?: number;
  tasks_failed_24h?: number;
  conversations?: number;
  credits_spent_7d?: number;
  credits_spent_30d?: number;
  frozen?: boolean;
  [k: string]: unknown;
}

export interface AdminUser {
  user_id: string;
  email: string | null;
  role: string;
  credits: number;
  stripe_customer_id: string | null;
  subscription_plan: string | null;
  subscription_status: string | null;
  created_at: string | null;
}

export interface AdminTaskRow {
  task_id: string;
  user_id: string | null;
  status: string | null;
  topic: string | null;
  tier: string | null;
  budget_usd: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AuditEntry {
  id: string;
  actor: string | null;
  action: string;
  target_type: string | null;
  target_id: string | null;
  before: unknown;
  after: unknown;
  meta: unknown;
  ip: string | null;
  ua: string | null;
  created_at: string;
}

export interface FeatureFlag {
  key: string;
  enabled: boolean;
  value: unknown;
  description: string | null;
  updated_at: string | null;
  updated_by: string | null;
}

export interface InternalAdminTask {
  id: string;
  title: string;
  description: string | null;
  category: string | null;
  priority: string | null;
  status: string | null;
  assignee: string | null;
  due_date: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface UsageRollupRow {
  day: string;
  user_id: string | null;
  tasks_total?: number;
  tasks_failed?: number;
  credits_spent?: number;
  cost_usd?: number;
  [k: string]: unknown;
}

export interface HealthProbeResult {
  ok: boolean;
  timestamp: string;
  components: Record<
    string,
    { ok: boolean; detail: string; latency_ms: number }
  >;
}

/* ---------- Endpoints ------------------------------------------------ */

export const adminApi = {
  overview: () => call<AdminOverview>("GET", "/api/admin/overview"),

  listUsers: () => call<AdminUser[]>("GET", "/api/admin/users"),

  setUserRole: (userId: string, role: "user" | "admin" | "banned") =>
    call<{ user_id: string; role: string }>(
      "POST",
      `/api/admin/users/${userId}/role`,
      { role },
    ),

  suspendUser: (userId: string, suspend: boolean, reason?: string) =>
    call<{ user_id: string; suspended: boolean }>(
      "POST",
      `/api/admin/users/${userId}/suspend`,
      { suspend, reason: reason ?? null },
    ),

  adjustCredits: (
    userId: string,
    mode: "set" | "delta",
    amount: number,
    reason?: string,
  ) =>
    call<{ user_id: string; new_balance: number }>(
      "POST",
      `/api/admin/users/${userId}/credits-v2`,
      { mode, amount, reason: reason ?? null },
    ),

  listTasks: (params: {
    status?: string;
    user_id?: string;
    limit?: number;
    offset?: number;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.user_id) q.set("user_id", params.user_id);
    q.set("limit", String(params.limit ?? 50));
    q.set("offset", String(params.offset ?? 0));
    return call<AdminTaskRow[]>("GET", `/api/admin/tasks?${q.toString()}`);
  },

  auditLog: (params: { limit?: number; offset?: number; action?: string } = {}) => {
    const q = new URLSearchParams();
    q.set("limit", String(params.limit ?? 100));
    q.set("offset", String(params.offset ?? 0));
    if (params.action) q.set("action", params.action);
    return call<AuditEntry[]>("GET", `/api/admin/audit-log?${q.toString()}`);
  },

  listFlags: () => call<FeatureFlag[]>("GET", "/api/admin/feature-flags"),

  upsertFlag: (body: {
    key: string;
    enabled: boolean;
    value?: unknown;
    description?: string;
  }) => call<FeatureFlag>("POST", "/api/admin/feature-flags", body),

  deleteFlag: (key: string) =>
    call<{ key: string; deleted: boolean }>(
      "DELETE",
      `/api/admin/feature-flags/${encodeURIComponent(key)}`,
    ),

  listInternalTasks: (params: {
    status?: string;
    category?: string;
    priority?: string;
  } = {}) => {
    const q = new URLSearchParams();
    if (params.status) q.set("status", params.status);
    if (params.category) q.set("category", params.category);
    if (params.priority) q.set("priority", params.priority);
    return call<InternalAdminTask[]>(
      "GET",
      `/api/admin/admin-tasks?${q.toString()}`,
    );
  },

  createInternalTask: (body: Partial<InternalAdminTask>) =>
    call<InternalAdminTask>("POST", "/api/admin/admin-tasks", body),

  patchInternalTask: (id: string, body: Partial<InternalAdminTask>) =>
    call<InternalAdminTask>("PATCH", `/api/admin/admin-tasks/${id}`, body),

  deleteInternalTask: (id: string) =>
    call<{ id: string; deleted: boolean }>(
      "DELETE",
      `/api/admin/admin-tasks/${id}`,
    ),

  usageRollup: (days = 30) =>
    call<UsageRollupRow[]>("GET", `/api/admin/usage?days=${days}`),

  healthProbe: () => call<HealthProbeResult>("GET", "/api/admin/health-probe"),

  setSystemFreeze: (frozen: boolean, reason?: string, message?: string) =>
    call<{ frozen: boolean; message: string | null }>(
      "POST",
      "/api/admin/system/freeze",
      { frozen, reason: reason ?? null, message: message ?? null },
    ),

  dangerFlushRedis: (confirm: string) =>
    call<{ flushed: boolean }>("POST", "/api/admin/danger/flush-redis", {
      confirm,
    }),

  dangerHaltRunning: (confirm: string) =>
    call<{ halted: number }>("POST", "/api/admin/danger/halt-running", {
      confirm,
    }),
};
