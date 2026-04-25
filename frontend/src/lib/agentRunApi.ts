/**
 * Agent run API — start, fetch, stream, stop.
 * Wraps the existing `/api/agent` family, mapping Deft's ModelTier to the
 * underlying selected_model + budget_usd that the orchestrator expects.
 */
import { api } from "@/lib/api";
import { supabase } from "@/lib/supabase";
import type { ModelTier } from "@/lib/agentApi";

export interface AgentStartParams {
  prompt: string;
  tier: ModelTier;
  /** Credit ceiling (1 credit == $0.01). */
  ceilingCredits: number;
  conversationId?: string | null;
  /**
   * F4 Vault: ephemeral env injection.  The frontend extracts $KEY_NAME
   * sentinels from the prompt, decrypts them locally with the user's vault
   * masterKey, and ships the resulting NAME→plaintext map alongside the
   * agent start request.  The server stores it in Redis with a TTL bounded
   * by the task's wall-clock budget and deletes it on terminal state.
   * Plaintext values NEVER touch localStorage / sessionStorage / disk.
   */
  vaultEnv?: Record<string, string>;
}

export interface AgentStartResponse {
  task_id: string;
  state: string;
  message: string;
}

const TIER_TO_MODEL: Record<ModelTier, string> = {
  lite: "claude-sonnet-4-6",
  standard: "claude-opus-4-7",
  max: "claude-opus-4-7",
};

export function startAgentRun(params: AgentStartParams): Promise<AgentStartResponse> {
  const budget_usd = Math.max(0.1, Math.min(100, params.ceilingCredits / 100));
  const body: Record<string, unknown> = {
    goal: params.prompt,
    selected_model: TIER_TO_MODEL[params.tier],
    budget_usd,
    max_duration_hours: params.tier === "max" ? 4 : params.tier === "standard" ? 2 : 1,
    conversation_id: params.conversationId ?? null,
  };
  if (params.vaultEnv && Object.keys(params.vaultEnv).length > 0) {
    body.vault_env = params.vaultEnv;
  }
  return api.post<AgentStartResponse>("/api/agent", body);
}

export interface AgentTaskState {
  id: string;
  user_id: string;
  goal: string;
  state: string;
  selected_model: string;
  budget_usd: number;
  spent_usd: number;
  steps?: Array<Record<string, unknown>>;
  artifacts?: Array<Record<string, unknown>>;
  final_answer?: string | null;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export function getAgentTask(taskId: string, signal?: AbortSignal): Promise<AgentTaskState> {
  return api.get<AgentTaskState>(`/api/agent/${encodeURIComponent(taskId)}`, { signal });
}

export interface AgentEvent {
  id: number;
  task_id: string;
  event_type: string;
  state: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface AgentEventsResponse {
  events: AgentEvent[];
  next_after_id: number | null;
}

export function getAgentEvents(
  taskId: string,
  afterId = 0,
  limit = 200,
  signal?: AbortSignal,
): Promise<AgentEventsResponse> {
  const qs = new URLSearchParams({ after_id: String(afterId), limit: String(limit) });
  return api.get<AgentEventsResponse>(
    `/api/agent/${encodeURIComponent(taskId)}/events?${qs.toString()}`,
    { signal },
  );
}

export function stopAgentRun(taskId: string): Promise<{ task_id: string; stopped: boolean; message: string }> {
  return api.post(`/api/agent/${encodeURIComponent(taskId)}/stop`);
}

/**
 * Open an EventSource for the agent's SSE stream.
 *
 * EventSource doesn't support custom headers, so we attach the access token
 * as a `?token=` query param — backend's `get_stream_user` accepts both.
 */
export async function openAgentStream(taskId: string): Promise<EventSource> {
  const { data } = await supabase.auth.getSession();
  const token = data.session?.access_token;
  if (!token) throw new Error("Not authenticated");
  const apiBase = (import.meta.env.VITE_API_URL ?? "").replace(/\/+$/, "");
  const url = `${apiBase}/api/agent/${encodeURIComponent(taskId)}/stream?token=${encodeURIComponent(token)}`;
  return new EventSource(url);
}
