/**
 * Dev-only Vault preview.
 *
 * Renders Vault sub-views with deterministic mock data so we can iterate on
 * visuals at any viewport without auth or a live vault.  Gated on
 * import.meta.env.DEV in App.tsx.
 *
 * ?mode=setup | unlock | unlocked_empty | unlocked_with_secrets | wizard_recovery
 *
 * Note: we DO NOT mount the real <Vault /> page here, because it depends on
 * useVault() / useAuth() singletons we'd have to monkey-patch.  Instead we
 * compose the same presentation primitives directly.
 */
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { VaultHeader } from "@/components/deft/vault/VaultHeader";
import { UnlockedBar } from "@/components/deft/vault/UnlockedBar";
import { SecretsTableView } from "@/components/deft/vault/SecretsTableView";
import { SecretsEmptyState } from "@/components/deft/vault/SecretsEmptyState";
import { Lock, ShieldCheck, AlertTriangle, Copy, Eye } from "lucide-react";
import type { SecretDTO } from "@/lib/vaultApi";

type Mode =
  | "setup"
  | "wizard_recovery"
  | "unlock"
  | "unlocked_empty"
  | "unlocked_with_secrets";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;

function mkSecret(
  id: string,
  name: string,
  description: string | null,
  ageMs: number,
): SecretDTO {
  const iso = new Date(Date.now() - ageMs).toISOString();
  // The DTO has crypto fields we don't need for the view; provide stubs so
  // TS is happy.  Empty strings are fine because SecretsTableView never
  // touches these fields.
  return {
    id,
    name,
    description,
    value_iv: "",
    value_blob: "",
    preview_iv: "",
    preview_blob: "",
    created_at: iso,
    updated_at: iso,
  };
}

const SECRETS: SecretDTO[] = [
  mkSecret("s1", "OPENAI_API_KEY", "Production GPT-4 key", 4 * HOUR),
  mkSecret("s2", "STRIPE_SECRET_KEY", "Live Stripe key", 2 * DAY),
  mkSecret("s3", "RESEND_API_KEY", "Transactional email", 5 * DAY),
  mkSecret("s4", "GITHUB_TOKEN", null, 18 * DAY),
  mkSecret("s5", "ANTHROPIC_API_KEY", "Claude Sonnet 4.6", 47 * 60_000),
];

const PREVIEWS: Record<string, string> = {
  s1: "yZQK",
  s2: "9hL3",
  s3: "0nNa",
  s4: "X2pq",
  s5: "Mwsd",
};

export default function DevVault() {
  if (!import.meta.env.DEV) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-foreground">
        <p className="text-sm text-muted-foreground">Dev preview disabled in production.</p>
      </div>
    );
  }
  const [params, setParams] = useSearchParams();
  const mode = (params.get("mode") as Mode | null) ?? "unlocked_with_secrets";
  const goto = (m: Mode) => {
    const next = new URLSearchParams(params);
    next.set("mode", m);
    setParams(next, { replace: true });
  };

  const autoLockAt = useMemo(
    () => new Date(Date.now() + 24 * 60_000).toISOString(),
    // refresh on mode change so the UI is deterministic per visit
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode],
  );

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <Navbar />
      <div className="mt-16 border-b border-border/60 bg-surface-1/40 px-3 py-2">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
          <span className="font-mono uppercase tracking-[0.16em]">/dev/vault</span>
          <span aria-hidden>·</span>
          <ModeBtn current={mode} value="setup" onClick={() => goto("setup")} />
          <ModeBtn
            current={mode}
            value="wizard_recovery"
            onClick={() => goto("wizard_recovery")}
          />
          <ModeBtn current={mode} value="unlock" onClick={() => goto("unlock")} />
          <ModeBtn
            current={mode}
            value="unlocked_empty"
            onClick={() => goto("unlocked_empty")}
          />
          <ModeBtn
            current={mode}
            value="unlocked_with_secrets"
            onClick={() => goto("unlocked_with_secrets")}
          />
        </div>
      </div>

      <main className="mx-auto w-full max-w-3xl flex-1 px-6 pt-6 pb-16">
        <VaultHeader />

        <div className="mt-6">
          {mode === "setup" && <MockSetupStep1 />}
          {mode === "wizard_recovery" && <MockSetupStep2 />}
          {mode === "unlock" && <MockUnlockCard />}
          {mode === "unlocked_empty" && (
            <div className="space-y-5">
              <UnlockedBar
                autoLockAt={autoLockAt}
                autoLockMinutes={30}
                onLock={() => alert("(dev) lock")}
                onRequestDestroy={() => alert("(dev) destroy")}
              />
              <SecretsEmptyState onAdd={() => alert("(dev) add")} />
            </div>
          )}
          {mode === "unlocked_with_secrets" && (
            <div className="space-y-5">
              <UnlockedBar
                autoLockAt={autoLockAt}
                autoLockMinutes={30}
                onLock={() => alert("(dev) lock")}
                onRequestDestroy={() => alert("(dev) destroy")}
              />
              <SecretsTableView
                secrets={SECRETS}
                previews={PREVIEWS}
                onCopyValue={() => Promise.resolve()}
                onCopySentinel={() => Promise.resolve()}
                onEdit={() => undefined}
                onDelete={() => undefined}
                onAdd={() => undefined}
              />
            </div>
          )}
        </div>
      </main>

      <Footer />
    </div>
  );
}

function ModeBtn({
  current,
  value,
  onClick,
}: {
  current: Mode;
  value: Mode;
  onClick: () => void;
}) {
  const active = current === value;
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "rounded px-2 py-0.5 font-mono uppercase tracking-[0.12em] transition-colors " +
        (active
          ? "bg-accent/20 text-accent"
          : "text-muted-foreground hover:bg-secondary hover:text-foreground")
      }
    >
      {value.replace(/_/g, " ")}
    </button>
  );
}

function MockSetupStep1() {
  // Visual mock of VaultSetupWizard step 1 — no real wiring.
  const [showPass, setShowPass] = useState(false);
  return (
    <Card className="mx-auto max-w-xl border-border">
      <CardHeader>
        <CardTitle className="text-base">Create your vault</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <p className="text-muted-foreground">
          Your vault is encrypted in your browser. We store the ciphertext only —
          your passphrase never leaves this device.
        </p>
        <div className="space-y-2">
          <Label htmlFor="dev-pass">Passphrase</Label>
          <div className="relative">
            <Input
              id="dev-pass"
              type={showPass ? "text" : "password"}
              defaultValue="correct horse battery staple"
              placeholder="At least 12 characters"
            />
            <button
              type="button"
              onClick={() => setShowPass((v) => !v)}
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground hover:text-foreground"
              aria-label={showPass ? "Hide passphrase" : "Show passphrase"}
            >
              <Eye size={14} aria-hidden />
            </button>
          </div>
        </div>
        <div className="space-y-2">
          <Label htmlFor="dev-confirm">Confirm passphrase</Label>
          <Input
            id="dev-confirm"
            type={showPass ? "text" : "password"}
            defaultValue="correct horse battery staple"
          />
        </div>
        <div className="flex justify-end">
          <Button>Continue</Button>
        </div>
      </CardContent>
    </Card>
  );
}

function MockSetupStep2() {
  // Visual mock of VaultSetupWizard step 2 — recovery code reveal.
  const RECOVERY = "K7XQ-9HM2-VPZD-3WLN-T8RF-6QY4";
  return (
    <Card className="mx-auto max-w-xl border-border">
      <CardHeader>
        <CardTitle className="text-base">Save your recovery code</CardTitle>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[13px] text-amber-200/90">
          <div className="mb-1 flex items-center gap-1.5 font-medium">
            <AlertTriangle size={14} aria-hidden /> Shown once. We cannot recover it.
          </div>
          <p className="text-amber-200/70">
            If you forget your passphrase, this is the only way to unlock your
            vault. Save it in a password manager or somewhere offline.
          </p>
        </div>
        <div className="rounded-lg border border-border bg-muted/40 p-4">
          <p className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">
            Recovery code
          </p>
          <div className="flex items-center gap-3">
            <code className="flex-1 break-all font-mono text-sm tracking-wide text-foreground">
              {RECOVERY}
            </code>
            <Button size="sm" variant="outline" aria-label="Copy recovery code">
              <Copy size={14} aria-hidden />
            </Button>
          </div>
        </div>
        <div className="space-y-2">
          <Label htmlFor="dev-confirm-code">
            Type the code back to confirm you've saved it
          </Label>
          <Input
            id="dev-confirm-code"
            placeholder="ABCD-EFGH-…"
            className="font-mono"
            autoComplete="off"
          />
        </div>
        <div className="flex justify-end">
          <Button>I've saved it — finish setup</Button>
        </div>
      </CardContent>
    </Card>
  );
}

function MockUnlockCard() {
  return (
    <Card className="mx-auto max-w-md border-border">
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          <Lock size={16} className="text-muted-foreground" aria-hidden />
          Unlock vault
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="space-y-4 text-sm">
          <p className="flex items-start gap-2 text-[12.5px] text-muted-foreground">
            <ShieldCheck
              size={13}
              className="mt-0.5 shrink-0 text-foreground/70"
              aria-hidden
            />
            <span>
              We never see your passphrase. The key is derived locally and held in
              memory until you lock or close this tab.
            </span>
          </p>
          <div
            role="tablist"
            aria-label="Unlock method"
            className="flex gap-1 rounded-md bg-muted/60 p-1"
          >
            <span className="flex-1 rounded bg-background px-3 py-1.5 text-center text-xs font-medium text-foreground shadow-sm">
              Passphrase
            </span>
            <span className="flex-1 rounded px-3 py-1.5 text-center text-xs font-medium text-muted-foreground">
              Recovery code
            </span>
          </div>
          <div className="space-y-2">
            <Label htmlFor="dev-unlock-pass">Passphrase</Label>
            <Input id="dev-unlock-pass" type="password" defaultValue="••••••••••••••" />
          </div>
          <div className="flex justify-end">
            <Button>
              <Lock size={14} className="mr-2" aria-hidden />
              Unlock
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
