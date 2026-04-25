import { describe, expect, it, vi } from "vitest";
import { scanVaultRefs, resolveVaultRefs, VaultRefError } from "./vaultPromptScan";

describe("scanVaultRefs", () => {
  it("returns empty for prompt without refs", () => {
    expect(scanVaultRefs("hello world")).toEqual({ names: [], occurrences: 0 });
  });

  it("finds a single ref", () => {
    expect(scanVaultRefs("use $OPENAI_API_KEY please")).toEqual({
      names: ["OPENAI_API_KEY"],
      occurrences: 1,
    });
  });

  it("dedupes repeated refs but counts occurrences", () => {
    const r = scanVaultRefs("$FOO and $FOO and $BAR");
    expect(r.names).toEqual(["BAR", "FOO"]);
    expect(r.occurrences).toBe(3);
  });

  it("does not match $$ESCAPED", () => {
    expect(scanVaultRefs("price is $$DOLLAR")).toEqual({ names: [], occurrences: 0 });
  });

  it("does not match identifier-glued $X", () => {
    expect(scanVaultRefs("foo$BAR baz")).toEqual({ names: [], occurrences: 0 });
  });

  it("requires uppercase grammar", () => {
    expect(scanVaultRefs("$lowercase or $Mixed").names).toEqual([]);
  });

  it("allows underscores and digits after first letter", () => {
    expect(scanVaultRefs("$API_KEY_1 next $X9").names).toEqual(["API_KEY_1", "X9"]);
  });

  it("does not match $1 (digit-first)", () => {
    expect(scanVaultRefs("$1 dollar").names).toEqual([]);
  });

  it("matches refs at start and end of string", () => {
    expect(scanVaultRefs("$A bcd $Z").names).toEqual(["A", "Z"]);
  });

  it("single-letter names are allowed (grammar permits)", () => {
    expect(scanVaultRefs("$A and $Z here").names).toEqual(["A", "Z"]);
  });
});

describe("resolveVaultRefs", () => {
  it("resolves all names in order", async () => {
    const decrypt = vi.fn(async (n: string) => `secret_${n}`);
    const out = await resolveVaultRefs(["A", "B"], decrypt);
    expect(out).toEqual({ A: "secret_A", B: "secret_B" });
    expect(decrypt).toHaveBeenCalledTimes(2);
  });

  it("throws VaultRefError naming the missing key", async () => {
    const decrypt = vi.fn(async (n: string) => {
      if (n === "MISSING") throw new Error("not found");
      return "ok";
    });
    await expect(resolveVaultRefs(["MISSING"], decrypt)).rejects.toMatchObject({
      name: "VaultRefError",
      missingName: "MISSING",
    });
  });

  it("returns empty object for empty list", async () => {
    const decrypt = vi.fn();
    const out = await resolveVaultRefs([], decrypt);
    expect(out).toEqual({});
    expect(decrypt).not.toHaveBeenCalled();
  });
});

describe("VaultRefError", () => {
  it("includes missing name in message", () => {
    const e = new VaultRefError("OPENAI_API_KEY", "boom");
    expect(e.message).toContain("$OPENAI_API_KEY");
    expect(e.missingName).toBe("OPENAI_API_KEY");
  });
});
