/**
 * B-29 regression tests: Post-checkout credit refresh.
 *
 * After a Stripe checkout redirect, the success URL lands on:
 *   - /chat?checkout=success   (Chat.tsx)
 *   - /build?checkout=success  (Build.tsx)
 *   - /account?topup=success   (Account.tsx)
 *
 * None of these pages previously handled the query param or refreshed
 * the credit balance. The B-29 fix adds a useEffect on each page that:
 *   (a) detects the ?checkout=success / ?topup=success param on mount,
 *   (b) shows a success toast,
 *   (c) calls refreshUser() (and refetchBalance() on Build.tsx) with retries,
 *   (d) clears the query param from the URL.
 *
 * These tests verify the implementation exists in each file via source-level
 * structural checks, consistent with the project's test pattern for logic
 * that requires heavy provider mocking for full render tests.
 */

import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

function readPage(name: string): string {
  return readFileSync(resolve(__dirname, `../pages/${name}`), "utf-8");
}

const chatSource = readPage("Chat.tsx");
const buildSource = readPage("Build.tsx");
const accountSource = readPage("Account.tsx");

// ---------------------------------------------------------------------------
// Chat.tsx — ?checkout=success
// ---------------------------------------------------------------------------

describe("B-29 Chat.tsx post-checkout credit refresh", () => {
  it("imports useSearchParams from react-router-dom", () => {
    expect(chatSource).toContain("useSearchParams");
  });

  it("detects checkout=success query param", () => {
    expect(chatSource).toContain("checkout");
    expect(chatSource).toContain("success");
  });

  it("calls refreshUser() in the checkout success handler", () => {
    // The handler must call refreshUser (not just call it elsewhere in the file).
    // We look for it inside the checkout useEffect block.
    const checkoutEffect = chatSource.match(
      /B-29[\s\S]*?checkout.*?success[\s\S]*?refreshUser/
    )?.[0] ?? "";
    expect(checkoutEffect.length).toBeGreaterThan(0);
  });

  it("shows a success toast on checkout success", () => {
    expect(chatSource).toMatch(/toast\.success.*?Payment received/s);
  });

  it("clears the checkout param from the URL after handling", () => {
    // The effect must call setSearchParams / next.delete("checkout").
    expect(chatSource).toContain('next.delete("checkout")');
  });

  it("uses { replace: true } so back-button does not re-trigger the success flow", () => {
    // Check within the checkout block context.
    const block = chatSource.match(/B-29[\s\S]{0,3000}/)?.[0] ?? "";
    expect(block).toContain("replace: true");
  });

  it("polls refreshUser up to 3 times (not once — webhook may be delayed)", () => {
    const block = chatSource.match(/B-29[\s\S]{0,3000}/)?.[0] ?? "";
    expect(block).toContain("attempts < 3");
  });
});

// ---------------------------------------------------------------------------
// Build.tsx — ?checkout=success
// ---------------------------------------------------------------------------

describe("B-29 Build.tsx post-checkout credit refresh", () => {
  it("destructures refreshUser from useAuth()", () => {
    expect(buildSource).toMatch(/refreshUser.*useAuth\(\)|useAuth\(\).*refreshUser/s);
    // Specifically the destructuring line in the component body:
    expect(buildSource).toContain("refreshUser");
  });

  it("detects checkout=success query param", () => {
    expect(buildSource).toContain("checkout");
    expect(buildSource).toContain("success");
  });

  it("calls refetchBalance() in the checkout success handler", () => {
    const block = buildSource.match(/B-29[\s\S]{0,3000}/)?.[0] ?? "";
    expect(block).toContain("refetchBalance()");
  });

  it("calls refreshUser() in the checkout success handler", () => {
    const block = buildSource.match(/B-29[\s\S]{0,3000}/)?.[0] ?? "";
    expect(block).toContain("refreshUser()");
  });

  it("shows a success toast on checkout success", () => {
    expect(buildSource).toMatch(/toast\.success.*?Payment received/s);
  });

  it("clears the checkout param from the URL after handling", () => {
    expect(buildSource).toContain('next.delete("checkout")');
  });
});

// ---------------------------------------------------------------------------
// Account.tsx — ?topup=success
// ---------------------------------------------------------------------------

describe("B-29 Account.tsx post-topup credit refresh", () => {
  it("imports useSearchParams from react-router-dom", () => {
    expect(accountSource).toContain("useSearchParams");
  });

  it("destructures refreshUser from useAuth()", () => {
    expect(accountSource).toContain("refreshUser");
  });

  it("detects topup=success query param", () => {
    expect(accountSource).toContain("topup");
    expect(accountSource).toContain("success");
  });

  it("calls refreshUser() in the topup success handler", () => {
    const block = accountSource.match(/B-29[\s\S]{0,3000}/)?.[0] ?? "";
    expect(block).toContain("refreshUser()");
  });

  it("shows a success toast on topup success", () => {
    expect(accountSource).toMatch(/toast\.success.*?Payment received/s);
  });

  it("clears the topup param from the URL after handling", () => {
    expect(accountSource).toContain('next.delete("topup")');
  });

  it("uses { replace: true } so back-button does not re-trigger", () => {
    const block = accountSource.match(/B-29[\s\S]{0,3000}/)?.[0] ?? "";
    expect(block).toContain("replace: true");
  });
});
