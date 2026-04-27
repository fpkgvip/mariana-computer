/**
 * B-09 — SSE JWT exposure regression tests.
 *
 * These tests verify:
 *  1. mintInvestigationStreamToken / mintAgentStreamToken return opaque tokens.
 *  2. openAgentStream uses a minted token, never the raw JWT.
 *  3. When mint fails, openAgentStream throws — never falls back to the JWT.
 *  4. URLs produced by the helpers never contain "eyJ" (raw-JWT sentinel).
 *  5. assertNoRawJwt catches URLs that do contain the sentinel.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  mintInvestigationStreamToken,
  mintAgentStreamToken,
  assertNoRawJwt,
} from "./streamAuth";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** A plausible-looking raw Supabase JWT (starts with "eyJ"). */
const FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEyMyJ9.FAKE_SIG";

/** A short-lived opaque stream token (does NOT start with "eyJ"). */
const FAKE_STREAM_TOKEN = "aGVsbG8td29ybGQtc3RyZWFtLXRva2Vu";

const TASK_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const API_URL = "https://api.example.com";

// ---------------------------------------------------------------------------
// global.fetch mock plumbing
// ---------------------------------------------------------------------------

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// 1. mintInvestigationStreamToken — returns short-lived opaque token
// ---------------------------------------------------------------------------

describe("mintInvestigationStreamToken", () => {
  it("returns the stream_token from a 200 response (never the raw JWT)", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ stream_token: FAKE_STREAM_TOKEN, expires_in_seconds: 120 }),
    });

    const token = await mintInvestigationStreamToken(API_URL, TASK_ID, FAKE_JWT);

    expect(token).toBe(FAKE_STREAM_TOKEN);
    expect(token).not.toContain("eyJ");
  });

  it("calls the correct endpoint with Bearer auth — never puts JWT in URL", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ stream_token: FAKE_STREAM_TOKEN, expires_in_seconds: 120 }),
    });

    await mintInvestigationStreamToken(API_URL, TASK_ID, FAKE_JWT);

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    // The URL must be the mint endpoint — NOT an SSE URL.
    expect(url).toContain(`/api/investigations/${TASK_ID}/stream-token`);
    // JWT travels in the Authorization header, NOT in the URL query string.
    expect(url).not.toContain("eyJ");
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(
      `Bearer ${FAKE_JWT}`,
    );
  });

  it("throws (does not return JWT) when the server returns non-OK status", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      text: async () => "backend down",
    });

    await expect(
      mintInvestigationStreamToken(API_URL, TASK_ID, FAKE_JWT),
    ).rejects.toThrow(/503/);
  });

  it("throws when response is OK but stream_token field is absent", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ expires_in_seconds: 120 }), // missing stream_token
    });

    await expect(
      mintInvestigationStreamToken(API_URL, TASK_ID, FAKE_JWT),
    ).rejects.toThrow(/stream_token/);
  });
});

// ---------------------------------------------------------------------------
// 2. mintAgentStreamToken — same contract for agent tasks
// ---------------------------------------------------------------------------

describe("mintAgentStreamToken", () => {
  it("returns the stream_token from a 200 response", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ stream_token: FAKE_STREAM_TOKEN, expires_in_seconds: 120 }),
    });

    const token = await mintAgentStreamToken(API_URL, TASK_ID, FAKE_JWT);
    expect(token).toBe(FAKE_STREAM_TOKEN);
  });

  it("calls POST /api/agent/{taskId}/stream-token with Bearer auth", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ stream_token: FAKE_STREAM_TOKEN, expires_in_seconds: 120 }),
    });

    await mintAgentStreamToken(API_URL, TASK_ID, FAKE_JWT);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toContain(`/api/agent/${TASK_ID}/stream-token`);
    // JWT must not be in the URL
    expect(url).not.toContain("eyJ");
    expect((init.headers as Record<string, string>)["Authorization"]).toBe(
      `Bearer ${FAKE_JWT}`,
    );
  });

  it("throws when mint fails — never falls back to returning the JWT", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 401,
      statusText: "Unauthorized",
      text: async () => "",
    });

    // Must throw — caller must NOT receive the raw JWT as a fallback.
    await expect(mintAgentStreamToken(API_URL, TASK_ID, FAKE_JWT)).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// 3. openAgentStream — uses stream token, never raw JWT; throws on failure
// ---------------------------------------------------------------------------

describe("openAgentStream", () => {
  it("uses the minted stream token in the EventSource URL, never the JWT", async () => {
    // Mock supabase.auth.getSession
    vi.doMock("@/lib/supabase", () => ({
      supabase: {
        auth: {
          getSession: async () => ({ data: { session: { access_token: FAKE_JWT } } }),
        },
      },
    }));

    // Mint succeeds
    fetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ stream_token: FAKE_STREAM_TOKEN, expires_in_seconds: 120 }),
    });

    // Mock EventSource
    const capturedUrls: string[] = [];
    vi.stubGlobal(
      "EventSource",
      class MockEventSource {
        constructor(url: string) {
          capturedUrls.push(url);
        }
      },
    );

    const { openAgentStream } = await import("./agentRunApi");
    await openAgentStream(TASK_ID);

    expect(capturedUrls).toHaveLength(1);
    const sseUrl = capturedUrls[0];
    // Stream token must be present
    expect(sseUrl).toContain(encodeURIComponent(FAKE_STREAM_TOKEN));
    // Raw JWT must NOT be in the URL
    expect(sseUrl).not.toContain("eyJ");
    assertNoRawJwt(sseUrl); // belt-and-suspenders assertion
  });

  it("throws when mint fails — never falls back to placing JWT in URL", async () => {
    vi.doMock("@/lib/supabase", () => ({
      supabase: {
        auth: {
          getSession: async () => ({ data: { session: { access_token: FAKE_JWT } } }),
        },
      },
    }));

    // Mint fails
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      text: async () => "backend down",
    });

    vi.stubGlobal(
      "EventSource",
      class MockEventSource {
        constructor(_url: string) {}
      },
    );

    const { openAgentStream } = await import("./agentRunApi");
    // Must throw — not fall through to a JWT-carrying EventSource.
    await expect(openAgentStream(TASK_ID)).rejects.toThrow();
  });
});

// ---------------------------------------------------------------------------
// 4. assertNoRawJwt — URL sentinel check
// ---------------------------------------------------------------------------

describe("assertNoRawJwt", () => {
  it("does not throw for clean URLs", () => {
    expect(() =>
      assertNoRawJwt(`https://api.example.com/stream?token=${FAKE_STREAM_TOKEN}`),
    ).not.toThrow();
  });

  it("throws when the URL contains the eyJ JWT prefix", () => {
    expect(() =>
      assertNoRawJwt(`https://api.example.com/stream?token=${FAKE_JWT}`),
    ).toThrow(/B-09/);
  });

  it("catches partial JWT leakage mid-URL", () => {
    expect(() =>
      assertNoRawJwt(`https://api.example.com/stream?token=eyJhbGci&other=1`),
    ).toThrow();
  });
});
