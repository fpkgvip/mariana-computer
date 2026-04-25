/**
 * VaultUnlockDialog — passphrase OR recovery-code unlock.
 *
 * Used both inline on the /vault page and as a modal forcing unlock when an
 * agent run references a vault secret.
 *
 * Polish:
 *   - Autofocus the active input on mode switch (passphrase ↔ recovery).
 *   - Submit on Enter, anywhere in the form.
 *   - Esc clears the field (modal owners handle Esc to dismiss).
 *   - Quiet, generic error messaging on bad key (never leaks which path failed).
 */
import { useEffect, useRef, useState } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useVault } from "@/hooks/useVault";
import { Eye, EyeOff, Loader2, Lock, ShieldCheck } from "lucide-react";
import { toast } from "sonner";

interface Props {
  /** Optional success callback (e.g. close modal). */
  onUnlocked?: () => void;
  /** When true, render compact (modal-friendly) without outer Card chrome. */
  bare?: boolean;
}

export function VaultUnlockDialog({ onUnlocked, bare = false }: Props) {
  const vault = useVault();
  const [mode, setMode] = useState<"passphrase" | "recovery">("passphrase");
  const [passphrase, setPassphrase] = useState("");
  const [recoveryCode, setRecoveryCode] = useState("");
  const [showPass, setShowPass] = useState(false);
  const [busy, setBusy] = useState(false);

  const passphraseRef = useRef<HTMLInputElement>(null);
  const recoveryRef = useRef<HTMLInputElement>(null);

  // Autofocus the active input on mode change.
  useEffect(() => {
    const t = window.setTimeout(() => {
      if (mode === "passphrase") passphraseRef.current?.focus();
      else recoveryRef.current?.focus();
    }, 30);
    return () => window.clearTimeout(t);
  }, [mode]);

  const handleSubmit = async () => {
    if (busy) return;
    setBusy(true);
    try {
      if (mode === "passphrase") {
        if (passphrase.length === 0) throw new Error("Enter your passphrase.");
        await vault.unlockPassphrase(passphrase);
      } else {
        if (recoveryCode.replace(/[\s-]/g, "").length !== 24) {
          throw new Error("Recovery code should be 24 characters (excluding hyphens).");
        }
        await vault.unlockRecovery(recoveryCode);
      }
      setPassphrase("");
      setRecoveryCode("");
      toast.success("Vault unlocked.");
      onUnlocked?.();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      // Generic phrasing for verifier mismatch — don't leak which key was wrong.
      if (/verifier|wrong/i.test(msg)) {
        toast.error("That's not the right key for this vault.");
      } else {
        toast.error(msg);
      }
    } finally {
      setBusy(false);
    }
  };

  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") void handleSubmit();
    else if (e.key === "Escape") {
      if (mode === "passphrase") setPassphrase("");
      else setRecoveryCode("");
    }
  };

  const body = (
    <div className="space-y-4 text-sm">
      <p className="flex items-start gap-2 text-[12.5px] text-muted-foreground">
        <ShieldCheck size={13} className="mt-0.5 shrink-0 text-foreground/70" aria-hidden />
        <span>
          We never see your passphrase. The key is derived locally and held in memory
          until you lock or close this tab.
        </span>
      </p>

      <div
        role="tablist"
        aria-label="Unlock method"
        className="flex gap-1 rounded-md bg-muted/60 p-1"
      >
        <button
          type="button"
          role="tab"
          aria-selected={mode === "passphrase"}
          onClick={() => setMode("passphrase")}
          className={`flex-1 rounded px-3 py-1.5 text-xs font-medium transition-colors ${
            mode === "passphrase"
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          Passphrase
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === "recovery"}
          onClick={() => setMode("recovery")}
          className={`flex-1 rounded px-3 py-1.5 text-xs font-medium transition-colors ${
            mode === "recovery"
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:text-foreground"
          }`}
        >
          Recovery code
        </button>
      </div>

      {mode === "passphrase" ? (
        <div className="space-y-2">
          <Label htmlFor="unlock-passphrase">Passphrase</Label>
          <div className="relative">
            <Input
              id="unlock-passphrase"
              ref={passphraseRef}
              type={showPass ? "text" : "password"}
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              onKeyDown={onKey}
              autoComplete="current-password"
            />
            <button
              type="button"
              onClick={() => setShowPass((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground hover:text-foreground"
              aria-label={showPass ? "Hide passphrase" : "Show passphrase"}
            >
              {showPass ? <EyeOff size={14} /> : <Eye size={14} />}
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-2">
          <Label htmlFor="unlock-recovery">Recovery code</Label>
          <Input
            id="unlock-recovery"
            ref={recoveryRef}
            value={recoveryCode}
            onChange={(e) => setRecoveryCode(e.target.value)}
            onKeyDown={onKey}
            autoComplete="off"
            spellCheck={false}
            placeholder="ABCD-EFGH-…"
            className="font-mono"
          />
          <p className="text-[11.5px] text-muted-foreground">
            Twenty-four characters from setup. Hyphens optional.
          </p>
        </div>
      )}

      <div className="flex justify-end">
        <Button onClick={handleSubmit} disabled={busy}>
          {busy ? (
            <Loader2 size={14} className="mr-2 animate-spin" aria-hidden />
          ) : (
            <Lock size={14} className="mr-2" aria-hidden />
          )}
          Unlock
        </Button>
      </div>
    </div>
  );

  if (bare) return body;

  return (
    <Card className="mx-auto max-w-md border-border">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Lock size={16} className="text-muted-foreground" aria-hidden />
          Unlock vault
        </CardTitle>
      </CardHeader>
      <CardContent>{body}</CardContent>
    </Card>
  );
}
