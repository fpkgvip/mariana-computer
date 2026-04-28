/**
 * /vault — Deft Vault page.
 *
 * Three states:
 *   1. No vault row on server → setup wizard.
 *   2. Vault exists, but locked → unlock card.
 *   3. Vault unlocked → header + unlocked bar + secrets table.
 */
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Navbar } from "@/components/Navbar";
import { Footer } from "@/components/Footer";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useAuth } from "@/contexts/AuthContext";
import { useVault } from "@/hooks/useVault";
import { VaultSetupWizard } from "@/components/deft/VaultSetupWizard";
import { VaultUnlockDialog } from "@/components/deft/VaultUnlockDialog";
import { SecretsTable } from "@/components/deft/SecretsTable";
import { VaultHeader } from "@/components/deft/vault/VaultHeader";
import { UnlockedBar } from "@/components/deft/vault/UnlockedBar";
import { VaultSkeleton } from "@/components/deft/vault/VaultSkeleton";
import { toast } from "sonner";

const LOCK_MS_FALLBACK = 30 * 60 * 1000;

export default function VaultPage() {
  const { user, loading: authLoading } = useAuth();
  const navigate = useNavigate();
  const vault = useVault();
  const [confirmDestroy, setConfirmDestroy] = useState(false);
  // We keep the wizard mounted until the user explicitly clicks "Open vault"
  // (onDone). This ensures the recovery-code reveal step is always visible
  // immediately after setup, not whisked away by a state transition.
  const [setupInProgress, setSetupInProgress] = useState(false);

  // Whenever there is no vault on the server, we are implicitly in setup mode.
  useEffect(() => {
    if (vault.loaded && !vault.exists) setSetupInProgress(true);
  }, [vault.loaded, vault.exists]);

  // Auth gate (matches the ProtectedRoute pattern used on /build /account etc).
  if (!authLoading && !user) {
    navigate("/login", { replace: true });
    return null;
  }

  const handleDestroy = async () => {
    try {
      await vault.destroyVault();
      toast.success("Vault deleted.");
      setConfirmDestroy(false);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Could not delete vault.");
    }
  };

  // Compute auto-lock target (best-effort) — reads the same env var useVault uses.
  const autoLockMinutes = (() => {
    const raw = (import.meta.env as { VITE_VAULT_LOCK_MS?: string }).VITE_VAULT_LOCK_MS;
    const n = raw ? Number(raw) : NaN;
    const ms = Number.isFinite(n) && n > 0 ? n : LOCK_MS_FALLBACK;
    return Math.round(ms / 60_000);
  })();

  let body: JSX.Element;
  if (!vault.loaded || authLoading) {
    body = <VaultSkeleton />;
  } else if (!vault.exists || setupInProgress) {
    body = <VaultSetupWizard onDone={() => setSetupInProgress(false)} />;
  } else if (!vault.unlocked) {
    body = <VaultUnlockDialog />;
  } else {
    body = (
      <div className="space-y-5">
        <UnlockedBar
          autoLockMinutes={autoLockMinutes}
          onLock={vault.lock}
          onRequestDestroy={() => setConfirmDestroy(true)}
        />
        <SecretsTable />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <Navbar />
      <main className="mx-auto max-w-3xl px-6 pt-24 pb-16">
        <VaultHeader />

        {vault.loadError && (
          <div
            role="alert"
            className="mt-5 rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-200"
          >
            {vault.loadError}
          </div>
        )}

        <div className="mt-6">{body}</div>
      </main>

      <AlertDialog
        open={confirmDestroy}
        onOpenChange={(v) => !v && setConfirmDestroy(false)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete your vault?</AlertDialogTitle>
            <AlertDialogDescription>
              This permanently destroys the vault and all secrets stored in it. You
              will need to set up a new vault and re-add every key. This cannot be
              undone.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={handleDestroy}
              className="bg-rose-500 text-white hover:bg-rose-600"
            >
              Delete vault
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <Footer />
    </div>
  );
}
