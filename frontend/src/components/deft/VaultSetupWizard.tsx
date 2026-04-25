/**
 * VaultSetupWizard — three-step setup flow:
 *   1. Choose a passphrase (≥12 chars, with confirm).
 *   2. Show recovery code ONCE; require user to confirm they've saved it
 *      (must type the code back in).
 *   3. Done. Vault is unlocked, masterKey held in memory.
 */
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useVault } from "@/hooks/useVault";
import { normalizeRecoveryCode } from "@/lib/vaultCrypto";
import { Copy, Check, Eye, EyeOff, Loader2, ShieldCheck, AlertTriangle } from "lucide-react";
import { toast } from "sonner";

interface Props {
  onDone: () => void;
}

export function VaultSetupWizard({ onDone }: Props) {
  const vault = useVault();
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [passphrase, setPassphrase] = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPass, setShowPass] = useState(false);
  const [busy, setBusy] = useState(false);
  const [recoveryCode, setRecoveryCode] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [confirmCode, setConfirmCode] = useState("");

  const passwordError = (() => {
    if (passphrase.length === 0) return null;
    if (passphrase.length < 12) return "Passphrase must be at least 12 characters.";
    if (confirm.length > 0 && passphrase !== confirm) return "Passphrases do not match.";
    return null;
  })();

  const canProceed1 = passphrase.length >= 12 && passphrase === confirm && !busy;

  const handleStep1 = async () => {
    if (!canProceed1) return;
    setBusy(true);
    try {
      const { recoveryCode: rc } = await vault.setup(passphrase);
      setRecoveryCode(rc);
      setStep(2);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Vault setup failed.");
    } finally {
      setBusy(false);
    }
  };

  const handleCopy = async () => {
    if (!recoveryCode) return;
    try {
      await navigator.clipboard.writeText(recoveryCode);
      setCopied(true);
      toast.success("Recovery code copied. Store it somewhere safe.");
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error("Could not copy. Select and copy the text manually.");
    }
  };

  const handleStep2 = () => {
    if (!recoveryCode) return;
    if (normalizeRecoveryCode(confirmCode) !== normalizeRecoveryCode(recoveryCode)) {
      toast.error("That doesn't match the recovery code shown above.");
      return;
    }
    setStep(3);
    // Clear secrets from memory.
    setPassphrase("");
    setConfirm("");
    setRecoveryCode(null);
    setConfirmCode("");
  };

  if (step === 3) {
    return (
      <Card className="mx-auto max-w-xl border-border">
        <CardContent className="flex flex-col items-center gap-4 px-6 py-10 text-center">
          <div className="rounded-full bg-primary/10 p-4">
            <ShieldCheck size={32} className="text-primary" />
          </div>
          <h3 className="text-lg font-semibold text-foreground">Vault ready</h3>
          <p className="max-w-sm text-sm text-muted-foreground">
            Your vault is unlocked and ready to hold API keys. Add your first secret below.
          </p>
          <Button onClick={onDone}>Open vault</Button>
        </CardContent>
      </Card>
    );
  }

  if (step === 2 && recoveryCode) {
    return (
      <Card className="mx-auto max-w-xl border-border">
        <CardHeader>
          <CardTitle className="text-base">Save your recovery code</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4 text-sm">
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[13px] text-amber-200/90">
            <div className="mb-1 flex items-center gap-1.5 font-medium">
              <AlertTriangle size={14} /> Shown once. We cannot recover it.
            </div>
            <p className="text-amber-200/70">
              If you forget your passphrase, this is the <em>only</em> way to unlock your vault.
              Save it in a password manager or somewhere offline.
            </p>
          </div>

          <div className="rounded-lg border border-border bg-muted/40 p-4">
            <p className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
              Recovery code
            </p>
            <div className="flex items-center gap-3">
              <code
                data-testid="vault-recovery-code"
                className="flex-1 break-all font-mono text-sm tracking-wide text-foreground"
              >
                {recoveryCode}
              </code>
              <Button size="sm" variant="outline" onClick={handleCopy} aria-label="Copy recovery code">
                {copied ? <Check size={14} /> : <Copy size={14} />}
              </Button>
            </div>
          </div>

          <div className="space-y-2">
            <Label htmlFor="confirm-code">Type the code back to confirm you've saved it</Label>
            <Input
              id="confirm-code"
              autoComplete="off"
              spellCheck={false}
              value={confirmCode}
              onChange={(e) => setConfirmCode(e.target.value)}
              placeholder="ABCD-EFGH-…"
              className="font-mono"
            />
          </div>

          <div className="flex justify-end">
            <Button onClick={handleStep2} disabled={confirmCode.length === 0}>
              I've saved it — finish setup
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="mx-auto max-w-xl border-border">
      <CardHeader>
        <CardTitle className="text-base">Create your vault</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-muted-foreground">
          Your vault is end-to-end encrypted in your browser. We store the ciphertext only —
          your passphrase never leaves this device.
        </p>

        <div className="space-y-2">
          <Label htmlFor="passphrase">Passphrase</Label>
          <div className="relative">
            <Input
              id="passphrase"
              type={showPass ? "text" : "password"}
              value={passphrase}
              onChange={(e) => setPassphrase(e.target.value)}
              autoComplete="new-password"
              spellCheck={false}
              placeholder="At least 12 characters"
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

        <div className="space-y-2">
          <Label htmlFor="confirm">Confirm passphrase</Label>
          <Input
            id="confirm"
            type={showPass ? "text" : "password"}
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            autoComplete="new-password"
            spellCheck={false}
          />
          {passwordError && (
            <p className="text-xs text-red-400">{passwordError}</p>
          )}
        </div>

        <div className="flex justify-end">
          <Button onClick={handleStep1} disabled={!canProceed1}>
            {busy ? <Loader2 size={14} className="mr-2 animate-spin" /> : null}
            Continue
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
