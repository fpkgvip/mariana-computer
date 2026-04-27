/**
 * O-01 regression tests for PreflightCard.
 *
 * Bug: PreflightCard previously derived
 *   ceilingMin = max(1, floor(quote.credits_min * 0.5))
 * which let the user pick / type / slide to a ceiling well below 100 credits
 * for lite-tier prompts. The backend always reserves
 *   max(100, int(budget_usd * 100))
 * so a sub-100 ceiling either triggered a false 402 rejection or silently
 * over-reserved up to the 100-credit floor.
 *
 * Fix: clamp ceilingMin to the shared CREDITS_MIN_RESERVATION constant
 * (100 credits == $1.00) and surface the floor in the UI caption so the
 * user is never surprised by a hidden over-reservation.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import React from "react";

import { CREDITS_MIN_RESERVATION } from "@/components/deft/studio/stage";

// Mock fetchQuote so the debounced effect in PreflightCard returns a
// deterministic sub-100-credit lite quote without hitting the network.
vi.mock("@/lib/agentApi", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/lib/agentApi")>();
  return {
    ...real,
    fetchQuote: vi.fn().mockResolvedValue({
      tier: "lite",
      credits_min: 40,
      credits_max: 80,
      eta_seconds_min: 30,
      eta_seconds_max: 90,
      complexity_score: 0.5,
      breakdown: {
        tier_baseline_credits: 60,
        tier_variance: 20,
        complexity_score: 0.5,
        ceiling_applied: null,
      },
    }),
  };
});

vi.mock("@/lib/analytics", () => ({ track: vi.fn() }));

import { PreflightCard } from "@/components/deft/PreflightCard";

async function renderWithQuote() {
  const onStart = vi.fn();
  render(
    <PreflightCard
      prompt="tiny lite prompt"
      onStart={onStart}
      balance={5000}
    />,
  );
  // Quote fetch is debounced 350ms.
  await waitFor(
    () => {
      // Once the quote resolves the numeric ceiling input renders.
      expect(
        screen.getByLabelText(/credit ceiling \(numeric\)/i),
      ).toBeInTheDocument();
    },
    { timeout: 2000 },
  );
  return { onStart };
}

describe("O-01 PreflightCard ceiling minimum", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("test_o01_ceiling_min_clamps_to_100: lite quote with credits_min=40 still floors the ceiling at 100", async () => {
    await renderWithQuote();
    const numeric = screen.getByLabelText(
      /credit ceiling \(numeric\)/i,
    ) as HTMLInputElement;
    // Buggy formula would give max(1, floor(40*0.5)) = 20.
    expect(Number(numeric.min)).toBe(CREDITS_MIN_RESERVATION);

    const slider = screen.getByLabelText(/^credit ceiling$/i) as HTMLInputElement;
    expect(Number(slider.min)).toBe(CREDITS_MIN_RESERVATION);
  });

  it("test_o01_ceiling_input_rejects_below_100: typing 50 is clamped up to 100", async () => {
    await renderWithQuote();
    const numeric = screen.getByLabelText(
      /credit ceiling \(numeric\)/i,
    ) as HTMLInputElement;
    await act(async () => {
      fireEvent.change(numeric, { target: { value: "50" } });
    });
    // The clamping handler bumps any sub-floor input back to the canonical floor.
    expect(Number(numeric.value)).toBeGreaterThanOrEqual(
      CREDITS_MIN_RESERVATION,
    );
  });

  it("test_o01_ceiling_label_shows_minimum: caption surfaces the canonical floor", async () => {
    await renderWithQuote();
    expect(
      screen.getByText(
        new RegExp(
          `minimum reservation\\s+${CREDITS_MIN_RESERVATION}\\s+credits`,
          "i",
        ),
      ),
    ).toBeInTheDocument();
  });
});
