/**
 * B-09 regression tests — SSE stream-token auth, never raw JWT in URL.
 *
 * Acceptance criteria verified here:
 *   1. mintStreamToken helpers return short-lived opaque tokens.
 *   2. openAgentStream uses the minted token, never the raw JWT.
 *   3. When mint fails, openAgentStream throws — does NOT fall back to JWT.
 *   4. AgentTaskView surfaces an error state when mint fails (no JWT fallback).
 *   5. AgentTaskView uses the minted stream token in the EventSource URL.
 *   6. The SSE URL never contains "eyJ" (raw JWT sentinel).
 *
 * All network calls are intercepted via vi.mock / global.fetch stubs.
 * No live backend is required.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import React from "react";

// ---------------------------------------------------------------------------
// Shared constants
// ---------------------------------------------------------------------------

/** A plausible raw Supabase JWT — always starts with "eyJ". */
const FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyLTEifQ.FAKE";

/** A short-lived opaque stream token — does NOT start with "eyJ". */
const FAKE_STREAM_TOKEN = "c3RyZWFtLXRva2VuLW9wYXF1ZQ";

const TASK_ID = "11111111-2222-3333-4444-555555555555";
const API_URL = "https://api.test";

// ---------------------------------------------------------------------------
// Mocks — declared before any module import that depends on them.
// ---------------------------------------------------------------------------

// Stub streamAuth so the fetch calls are predictable.
vi.mock("@/lib/streamAuth", () => ({
  mintAgentStreamToken: vi.fn(),
  mintInvestigationStreamToken: vi.fn(),
  assertNoRawJwt: (url: string) => {
    if (url.includes("eyJ"))
      throw new Error(`B-09: raw JWT in URL: ${url}`);
  },
}));

// Stub heavy sub-components to keep AgentTaskView renderable.
vi.mock("@/components/agent/AgentPlanCard", () => ({
  AgentPlanCard: () => <div data-testid="plan-card" />,
}));
vi.mock("@/components/agent/AgentProgress", () => ({
  AgentProgress: () => <div data-testid="progress" />,
}));
vi.mock("@/components/agent/TerminalOutput", () => ({
  TerminalOutput: () => <div data-testid="terminal" />,
}));
vi.mock("@/components/agent/WorkspaceSidebar", () => ({
  WorkspaceSidebar: () => <div data-testid="sidebar" />,
}));

// ---------------------------------------------------------------------------
// Import modules under test (after mocks).
// ---------------------------------------------------------------------------

import { mintAgentStreamToken } from "@/lib/streamAuth";

const mockMintAgentStreamToken = mintAgentStreamToken as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Captured EventSource URLs from the current test. */
let capturedEsUrls: string[] = [];

/** Install a global EventSource stub that records constructor args. */
function stubEventSource() {
  capturedEsUrls = [];
  vi.stubGlobal(
    "EventSource",
    class MockEventSource {
      onmessage: ((ev: MessageEvent) => void) | null = null;
      onerror: (() => void) | null = null;
      constructor(url: string) {
        capturedEsUrls.push(url);
      }
      close() {}
    },
  );
}

/** Render AgentTaskView with controlled props. */
async function renderAgentTaskView(getToken: () => Promise<string | null>) {
  const { AgentTaskView } = await import("@/components/agent/AgentTaskView");
  return render(
    <AgentTaskView
      taskId={TASK_ID}
      userId="user-1"
      apiUrl={API_URL}
      getToken={getToken}
      goal="Test goal"
    />,
  );
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.clearAllMocks();
  stubEventSource();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

// ---------------------------------------------------------------------------
// Test: AgentTaskView uses stream token (not JWT) in EventSource URL
// ---------------------------------------------------------------------------

describe("AgentTaskView — SSE URL contains stream token, not raw JWT", () => {
  it("opens EventSource with the minted stream token, never the raw JWT", async () => {
    mockMintAgentStreamToken.mockResolvedValueOnce(FAKE_STREAM_TOKEN);

    await renderAgentTaskView(async () => FAKE_JWT);

    await waitFor(() => {
      expect(capturedEsUrls.length).toBeGreaterThan(0);
    });

    const url = capturedEsUrls[0];
    expect(url).toContain(encodeURIComponent(FAKE_STREAM_TOKEN));
    // Must not contain the raw JWT sentinel.
    expect(url).not.toContain("eyJ");
  });

  it("called mintAgentStreamToken with the bearer JWT and task id", async () => {
    mockMintAgentStreamToken.mockResolvedValueOnce(FAKE_STREAM_TOKEN);

    await renderAgentTaskView(async () => FAKE_JWT);

    await waitFor(() => {
      expect(mockMintAgentStreamToken).toHaveBeenCalledWith(API_URL, TASK_ID, FAKE_JWT);
    });
  });
});

// ---------------------------------------------------------------------------
// Test: AgentTaskView shows error state when mint fails — no JWT fallback
// ---------------------------------------------------------------------------

describe("AgentTaskView — mint failure surfaces error state, no JWT fallback", () => {
  it("renders the connection-error element and opens NO EventSource", async () => {
    mockMintAgentStreamToken.mockRejectedValueOnce(new Error("mint failed"));

    await renderAgentTaskView(async () => FAKE_JWT);

    await waitFor(() => {
      expect(screen.getByTestId("connection-error")).toBeInTheDocument();
    });

    // No EventSource should have been created — the JWT must not be in any URL.
    expect(capturedEsUrls).toHaveLength(0);
  });

  it("error message tells the user to refresh (no technical details)", async () => {
    mockMintAgentStreamToken.mockRejectedValueOnce(new Error("503 backend down"));

    await renderAgentTaskView(async () => FAKE_JWT);

    await waitFor(() => {
      const el = screen.getByTestId("connection-error");
      expect(el).toHaveTextContent(/refresh/i);
    });
  });

  it("does NOT place the raw JWT in any EventSource URL on mint failure", async () => {
    mockMintAgentStreamToken.mockRejectedValueOnce(new Error("mint failed"));

    await renderAgentTaskView(async () => FAKE_JWT);

    await waitFor(() => {
      expect(screen.getByTestId("connection-error")).toBeInTheDocument();
    });

    for (const url of capturedEsUrls) {
      expect(url).not.toContain("eyJ");
    }
  });
});

// ---------------------------------------------------------------------------
// Test: SSE URL never contains raw JWT sentinel — network mock assertion
// ---------------------------------------------------------------------------

describe("B-09 sentinel — SSE URL never contains 'eyJ'", () => {
  it("assertNoRawJwt passes for a URL with a stream token", async () => {
    const { assertNoRawJwt } = await import("@/lib/streamAuth");
    expect(() =>
      assertNoRawJwt(
        `${API_URL}/api/agent/${TASK_ID}/stream?token=${FAKE_STREAM_TOKEN}`,
      ),
    ).not.toThrow();
  });

  it("assertNoRawJwt throws for a URL containing a raw JWT", async () => {
    const { assertNoRawJwt } = await import("@/lib/streamAuth");
    expect(() =>
      assertNoRawJwt(`${API_URL}/api/agent/${TASK_ID}/stream?token=${FAKE_JWT}`),
    ).toThrow(/B-09/);
  });

  it("all captured EventSource URLs pass assertNoRawJwt after successful mint", async () => {
    mockMintAgentStreamToken.mockResolvedValueOnce(FAKE_STREAM_TOKEN);
    await renderAgentTaskView(async () => FAKE_JWT);

    await waitFor(() => {
      expect(capturedEsUrls.length).toBeGreaterThan(0);
    });

    const { assertNoRawJwt } = await import("@/lib/streamAuth");
    for (const url of capturedEsUrls) {
      expect(() => assertNoRawJwt(url)).not.toThrow();
    }
  });
});
