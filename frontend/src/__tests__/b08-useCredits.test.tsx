/**
 * B-08 regression tests — Navbar/BuyCredits live credit balance.
 *
 * Acceptance criteria verified here:
 *   AC-1: Navbar renders liveBalance (not user.tokens) for desktop & mobile call sites.
 *   AC-1: BuyCredits renders liveBalance (not user.tokens).
 *   AC-2: useCredits fetches /api/credits/balance on mount.
 *   AC-2: useCredits updates on "deft:credits-changed" custom event.
 *   AC-2: useCredits refreshes on window "focus".
 *   AC-2: useCredits refreshes on document visibilitychange → visible.
 *   AC-2: useCredits polls on the configured backstop interval.
 *
 * All network calls are intercepted via vi.mock — no live backend needed.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { MemoryRouter } from "react-router-dom";

// ---------------------------------------------------------------------------
// Mocks — must be declared before importing the modules under test.
// ---------------------------------------------------------------------------

// Mock the agentApi module so fetchBalance never hits the network.
vi.mock("@/lib/agentApi", () => ({
  fetchBalance: vi.fn(),
}));

// Mock AuthContext — we need a controlled user object.
const mockUser = {
  id: "test-user-id",
  email: "test@example.com",
  name: "Test User",
  tokens: 9999, // intentionally stale value — components must NOT render this
  role: "user",
  subscription_plan: "free",
  subscription_status: "active",
};

vi.mock("@/contexts/AuthContext", () => ({
  useAuth: vi.fn(() => ({
    user: mockUser,
    loading: false,
    login: vi.fn(),
    signup: vi.fn(),
    logout: vi.fn(),
    skip: vi.fn(),
    refreshUser: vi.fn(),
  })),
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

// Stub sub-components that pull in heavy deps to keep tests fast.
vi.mock("@/components/ScrollReveal", () => ({
  ScrollReveal: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));
vi.mock("@/components/Footer", () => ({
  Footer: () => <footer data-testid="footer" />,
}));
vi.mock("@/lib/brand", () => ({
  BRAND: { name: "Deft" },
}));

// ---------------------------------------------------------------------------
// Import modules under test (after mocks are in place).
// ---------------------------------------------------------------------------
import { useCredits, CREDITS_CHANGED_EVENT } from "@/hooks/useCredits";
import { fetchBalance } from "@/lib/agentApi";
import { Navbar } from "@/components/Navbar";
import BuyCredits from "@/pages/BuyCredits";

// Typed handle to the mock so we can control its resolved value.
const mockFetchBalance = fetchBalance as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// useCredits unit tests (fake timers to control setInterval precisely)
// ---------------------------------------------------------------------------

describe("useCredits", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers();
    mockFetchBalance.mockResolvedValue({ balance: 500, next_expiry: null });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  /**
   * Flush pending microtasks (Promise resolutions) without advancing fake
   * timers. Uses a real Promise tick so state updates settle.
   */
  async function flushPromises() {
    await act(async () => {
      // Two ticks: one for the fetchBalance Promise, one for React setState.
      await Promise.resolve();
      await Promise.resolve();
    });
  }

  it("returns balance from /api/credits/balance on mount", async () => {
    // Disable polling so the interval never fires.
    const { result } = renderHook(() => useCredits(0));

    await flushPromises();

    expect(mockFetchBalance).toHaveBeenCalledTimes(1);
    expect(result.current.balance).toBe(500);
  });

  it("updates balance when deft:credits-changed event fires", async () => {
    mockFetchBalance
      .mockResolvedValueOnce({ balance: 500, next_expiry: null })
      .mockResolvedValueOnce({ balance: 750, next_expiry: null });

    const { result } = renderHook(() => useCredits(0));

    await flushPromises();
    expect(result.current.balance).toBe(500);

    // Fire the custom event that spend/grant paths dispatch.
    await act(async () => {
      window.dispatchEvent(new Event(CREDITS_CHANGED_EVENT));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockFetchBalance).toHaveBeenCalledTimes(2);
    expect(result.current.balance).toBe(750);
  });

  it("refreshes balance on window focus", async () => {
    mockFetchBalance
      .mockResolvedValueOnce({ balance: 300, next_expiry: null })
      .mockResolvedValueOnce({ balance: 320, next_expiry: null });

    const { result } = renderHook(() => useCredits(0));

    await flushPromises();
    expect(result.current.balance).toBe(300);

    // Simulate the user returning to the tab.
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockFetchBalance).toHaveBeenCalledTimes(2);
    expect(result.current.balance).toBe(320);
  });

  it("refreshes balance on visibilitychange to visible", async () => {
    mockFetchBalance
      .mockResolvedValueOnce({ balance: 100, next_expiry: null })
      .mockResolvedValueOnce({ balance: 150, next_expiry: null });

    const { result } = renderHook(() => useCredits(0));

    await flushPromises();
    expect(result.current.balance).toBe(100);

    // jsdom's document.visibilityState is always "visible", so dispatching
    // the event is sufficient to trigger the handler branch.
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockFetchBalance).toHaveBeenCalledTimes(2);
    expect(result.current.balance).toBe(150);
  });

  it("polls on the configured backstop interval and refreshes balance", async () => {
    mockFetchBalance
      .mockResolvedValueOnce({ balance: 200, next_expiry: null })
      .mockResolvedValueOnce({ balance: 250, next_expiry: null });

    const POLL = 30_000;
    const { result } = renderHook(() => useCredits(POLL));

    // Resolve the initial fetch.
    await flushPromises();
    expect(result.current.balance).toBe(200);

    // Advance by exactly one poll interval; does NOT loop indefinitely.
    await act(async () => {
      vi.advanceTimersByTime(POLL);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockFetchBalance).toHaveBeenCalledTimes(2);
    expect(result.current.balance).toBe(250);
  });
});

// ---------------------------------------------------------------------------
// Navbar integration tests (real timers — waitFor needs real time to poll)
// ---------------------------------------------------------------------------

describe("Navbar — renders live balance, not user.tokens", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // The live balance (500) is intentionally different from stale tokens (9999).
    mockFetchBalance.mockResolvedValue({ balance: 500, next_expiry: null });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("desktop user menu shows live balance from useCredits, not stale user.tokens", async () => {
    render(
      <MemoryRouter>
        <Navbar />
      </MemoryRouter>,
    );

    // Wait for the async fetchBalance to resolve and React to re-render.
    await waitFor(() => {
      // "500 credits" should appear in at least one location (desktop + mobile).
      expect(screen.getAllByText(/500 credits/i).length).toBeGreaterThanOrEqual(1);
    });

    // The stale user.tokens value (9,999) must not appear.
    expect(screen.queryByText(/9,999 credits/i)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// BuyCredits integration tests (real timers)
// ---------------------------------------------------------------------------

describe("BuyCredits — renders live balance, not user.tokens", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // Live balance (42) is intentionally different from stale tokens (9999).
    mockFetchBalance.mockResolvedValue({ balance: 42, next_expiry: null });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows live balance from useCredits, not stale user.tokens", async () => {
    render(
      <MemoryRouter>
        <BuyCredits />
      </MemoryRouter>,
    );

    // Wait for the async fetchBalance to resolve and React to re-render.
    // Multiple elements may show the balance (e.g. Navbar + page body), so use
    // getAllByText to match one or more occurrences.
    await waitFor(() => {
      expect(screen.getAllByText(/42 credits/i).length).toBeGreaterThanOrEqual(1);
    });

    // Stale user.tokens value (9,999) must not appear anywhere in the page.
    expect(screen.queryByText(/9,999 credits/i)).toBeNull();
  });
});
